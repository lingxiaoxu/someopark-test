"""
AISSdailySignal.py — 板块轮动每日信号生成器
=======================================================
Production daily runner for the AISS strategy.

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
    python qlib-main/semiconductor_strategy/AISSdailySignal.py \\
    --capital 1000000 [--date YYYY-MM-DD] [--dry-run] [--force-rebalance]
      [--value-source proxy]

目录（相对于 someopark-test/）：
  qlib-main/semiconductor_strategy/AISSdailySignal.py  ← 本文件
  qlib-main/semiconductor_strategy/inventory_aiss.json
  qlib-main/semiconductor_strategy/trading_signals/              ← JSON + TXT 报告
  qlib-main/semiconductor_strategy/inventory_history/            ← 历史快照
  qlib-main/semiconductor_strategy/data/cache/                   ← ETF 价格缓存
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
_THIS_DIR    = Path(__file__).parent.resolve()   # semiconductor_strategy/
_QLIB_DIR    = _THIS_DIR.parent.resolve()        # qlib-main/
_PROJECT_DIR = _QLIB_DIR.parent.resolve()        # someopark-test/

sys.path.insert(0, str(_QLIB_DIR))      # semiconductor_strategy.* imports
sys.path.insert(0, str(_PROJECT_DIR))   # MacroStateStore

# ── Sector-rotation module imports ───────────────────────────────────────────
from semiconductor_strategy.data.loader import load_config, load_prices, load_stock_prices
from semiconductor_strategy.data.universe import get_tickers, all_tickers
from semiconductor_strategy.stock_decompose import (
    decompose_to_stocks,
    build_stock_trades,
    stock_holdings_from_by_ticker,
    recently_unavailable,
)
from semiconductor_strategy.signals.composite import compute_composite_signals
from semiconductor_strategy.portfolio.optimizer import optimize_weights
from semiconductor_strategy.portfolio.risk import apply_risk_controls
from semiconductor_strategy.portfolio.rebalance import (
    compute_turnover,
    get_first_trading_day_of_month,
    should_emergency_rebalance,
)
from semiconductor_strategy.backtest.costs import compute_transaction_costs

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
log = logging.getLogger("AISSdailySignal")

# ── Constants ─────────────────────────────────────────────────────────────────
INVENTORY_PATH       = _THIS_DIR / "inventory_aiss.json"
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


def _load_ledger_equity_curve(min_points: int = 2):
    """实盘 DD 断路器净值源(2026-07-22): account_history 每日账本快照的真实 equity。

    回测-实盘不对称修复: MCPS 按带断路器保护的回测曲线选参,实盘此前不执行断路器。
    快照缺失/过短时返回 None → 断路器保持不触发(优雅降级,行为同修复前)。
    关闭开关: config risk.drawdown.live_dd_enabled: false
    """
    try:
        rows = {}
        for p in sorted((_THIS_DIR / "account_history").glob("account_aiss_*.json")):
            try:
                d = json.loads(p.read_text())
                if d.get("as_of") and d.get("equity") is not None:
                    rows[pd.Timestamp(d["as_of"])] = float(d["equity"])
            except Exception:
                continue
        cur = _THIS_DIR / "account_aiss.json"
        if cur.exists():
            d = json.loads(cur.read_text())
            if d.get("as_of") and d.get("equity") is not None:
                rows[pd.Timestamp(d["as_of"])] = float(d["equity"])
        if len(rows) < min_points:
            return None
        return pd.Series(rows).sort_index()
    except Exception:
        return None


def save_inventory(inv: dict, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] Inventory not saved.")
        return
    INVENTORY_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = INVENTORY_HISTORY_DIR / f"inventory_aiss_{ts}.json"
    with open(bak, "w") as f:
        json.dump(inv, f, indent=2)
    with open(INVENTORY_PATH, "w") as f:
        json.dump(inv, f, indent=2)
    log.info(f"Inventory saved → {INVENTORY_PATH.name}  (backup: {bak.name})")


# ─────────────────────────────────────────────────────────────────────────────
# Macro data loading  (MacroStateStore → 不重复下载，直接读 price_data/macro/)
# ─────────────────────────────────────────────────────────────────────────────

def _macro_store_target_date(end) -> pd.Timestamp:
    """
    自愈更新的"目标交易日"：min(end, 今天) 当日或之前最近的一个 NYSE 交易日。

    谨慎设计（避免误触发 / 引入未来信息）：
      • 历史 / as-of 回测（end 在过去）→ 目标落在过去，store 必已覆盖 → 不触发更新。
      • 周末 / 假日运行（end 非交易日）→ 回退到上一交易日，store 已有 → 不触发。
      • end 误配为未来 → clamp 到今天，绝不尝试抓取未来数据。
    返回 normalize 后的 Timestamp。
    """
    today = pd.Timestamp(date.today())
    cap = today if not end else min(pd.Timestamp(end).normalize(), today)
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(
            start_date=(cap - pd.Timedelta(days=12)).strftime("%Y-%m-%d"),
            end_date=cap.strftime("%Y-%m-%d"),
        )
        if not sched.empty:
            return pd.Timestamp(sched.index[-1]).normalize()
    except Exception:
        pass
    return cap


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

        # ── Self-heal：store 落后于目标交易日时，触发一次与 pre_pipeline 完全相同的
        #    MacroStateStore.update()（即 `MacroStateStore.py --update`），再重读一次。
        #    只补一次、绝不循环；补完仍落后就用现有数据并告警（绝不让 signal 失败）。
        #    目标日设计见 _macro_store_target_date：历史/as-of 回测不会触发（无未来信息）。
        _target = _macro_store_target_date(end)
        _store_last = pd.Timestamp(df.index[-1]).normalize() if len(df) else None
        if _store_last is None or _store_last < _target:
            log.warning(
                f"MacroStateStore last="
                f"{_store_last.date() if _store_last is not None else None} < target "
                f"{_target.date()} — triggering one-time store.update() "
                f"(same path as pre_pipeline 'MacroStateStore.py --update')"
            )
            try:
                store.update()
                df = store.load(start, end)   # 重读一次
                _store_last = pd.Timestamp(df.index[-1]).normalize() if len(df) else None
            except Exception as _e:
                log.warning(f"MacroStateStore.update() failed: {_e}; proceeding with available macro")
            if _store_last is None or _store_last < _target:
                log.warning(
                    f"MacroStateStore still behind after update "
                    f"(last={_store_last.date() if _store_last is not None else None}, "
                    f"target={_target.date()}); proceeding with available data"
                )

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
    from semiconductor_strategy.data.loader import load_macro_data
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

def _mid_month_trading_day(year: int, month: int):
    """~Mid-month rebalance day for V2 = the 10th NYSE trading day of the month
    (matches the backtest engine's V2 mid-month point; ~calendar 14th-15th)."""
    try:
        import calendar as _cal
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        last = _cal.monthrange(year, month)[1]
        sched = nyse.schedule(start_date=f"{year}-{month:02d}-01",
                              end_date=f"{year}-{month:02d}-{last:02d}")
        days = list(sched.index.date)
        return days[9] if len(days) > 9 else None
    except Exception:
        return None


def _should_rebalance(
    signal_date: date,
    inv: dict,
    macro_recent: pd.DataFrame,
    cfg: dict,
    force: bool = False,
    emergency_active: bool = False,
    vol_derisk_ctx: Optional[dict] = None,   # 防线 A: {'triggered': bool, 'target_cash': float}
) -> Tuple[bool, str]:
    """
    Returns (should_rebalance: bool, reason: str).
    Reasons: 'first_run' | 'monthly_rebalance' | 'semimonthly_rebalance' |
             'emergency_vix' | 'no_rebalance' | 'forced'
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

    # V2 semi-monthly: also rebalance at ~mid-month
    if cfg.get("signals", {}).get("signal_version", "v1") == "v2":
        mid = _mid_month_trading_day(signal_date.year, signal_date.month)
        if mid is not None and signal_date == mid:
            return True, "semimonthly_rebalance"

    # ── 防线 A（RISK_DEFENSE plan §1）：vol-scaling 连续触发的临时降险调仓 ──
    # 背景：2026-06-23~30 vol-scaling 连续 6 个交易日要求半仓、被月度节奏挡住，
    # 满仓穿越 7/1 崩盘。只在常规调仓日之外补位（放在 monthly/semimonthly 之后：调仓日走原 reason，目标现金本就会被应用）。
    # 守卫：连续 K=3 天触发（防单日噪声）+ 现金缺口 >20pp（天然只降不升）
    # + 每月最多 2 次（防抖动）。streak 由 daily 路径幂等维护（含今日 +1）。
    if vol_derisk_ctx and vol_derisk_ctx.get("triggered"):
        streak_today = int(inv.get("vol_derisk_streak", 0) or 0) + 1   # 含今日
        gap = (float(vol_derisk_ctx.get("target_cash") or 0)
               - float(inv.get("cash_weight", 0) or 0))
        n_month = sum(1 for r in inv.get("rebalance_history", [])
                      if r.get("reason") == "vol_derisk"
                      and str(r.get("date", ""))[:7] == signal_date.strftime("%Y-%m"))
        if streak_today >= 3 and gap > 0.20 and n_month < 2:
            return True, "vol_derisk"

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

def _reanchor_subsector_holdings(holdings: dict, subsector_prices: pd.DataFrame,
                                 capital: float) -> None:
    """把子板块簿记字段统一重锚定到当前 vintage 的合成指数序列（in-place）。

    子板块价格是收益复利合成指数，每次运行从个股 store 重建。成分股拆股的
    回溯调整（KLAC 1:10 @2026-06-12）或 store 历史变化会整体平移指数水平
    （vintage 漂移）——冻结在 inventory 里的 cost_basis 与新 vintage 的
    last_price 错配（equipment 曾显示 +130% 假盈亏）。修法 = 锚定原则
    （镜像 CorporateActions 守卫 4）：三个字段每次都从当前序列按语义日期重取：
      cost_basis = 指数在 entry_date 的水平
      last_price = 指数最新水平
      shares     = weight × capital ÷ 指数在 last_rebalance_date 的水平
    展示层已改用个股聚合；这些字段是内部簿记，重导出无副作用、幂等。
    """
    cols = list(getattr(subsector_prices, "columns", []))
    for sub, h in (holdings or {}).items():
        if sub not in cols:
            continue
        s = subsector_prices[sub].dropna()
        if s.empty:
            continue
        try:
            h["last_price"] = round(float(s.iloc[-1]), 4)
            entry = h.get("entry_date") or ""
            if entry:
                cb = s.loc[:entry]
                if len(cb):
                    h["cost_basis"] = round(float(cb.iloc[-1]), 4)
            reb = h.get("last_rebalance_date") or entry
            if reb and h.get("weight"):
                rb = s.loc[:reb]
                if len(rb) and float(rb.iloc[-1]) > 0:
                    h["shares"] = int(float(h["weight"]) * float(capital)
                                      / float(rb.iloc[-1]))
        except Exception:
            continue


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
    force: bool = False,
) -> dict:
    """
    Update inventory with today's positions.
    幂等：若 last_updated == signal_date，跳过更新——除非 force=True（强制调仓）。
    A forced rebalance must persist even on a same-day re-run (e.g. an operator
    applying a new graph after the daily cron already ran), so it bypasses the
    idempotency guard.
    """
    today_str = signal_date.isoformat()

    if inv.get("last_updated") == today_str and not force:
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
    path = SIGNALS_DIR / f"aiss_daily_report_{signal_date.strftime('%Y%m%d')}_{ts}.json"

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
    path = SIGNALS_DIR / f"aiss_daily_report_{signal_date.strftime('%Y%m%d')}_{ts}.txt"

    lines = []
    sep = "=" * 64

    lines.append(sep)
    lines.append(f"  AISS DAILY SIGNAL  —  {signal_date}")
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

    # ── Stock-level execution layer (below subsectors) ───────────
    breakdown = report.get("stock_breakdown", [])
    if breakdown:
        lines.append("")
        lines.append("  STOCK-LEVEL TARGET HOLDINGS  (executable layer below subsectors)")
        lines.append("  " + "-" * 72)
        lines.append(f"  {'SUBSECTOR':<15} {'STOCK':<6} {'TIER':<8} {'WITHIN%':>8} {'PORT%':>7} {'SHARES':>7} {'PRICE':>9}")
        # group rows by subsector, ordered by descending subsector port weight
        from collections import OrderedDict as _OD
        _by_sub = _OD()
        for r in breakdown:
            _by_sub.setdefault(r["subsector"], []).append(r)
        _sub_order = sorted(_by_sub, key=lambda s: -sum(x["portfolio_weight"] for x in _by_sub[s]))
        for sub in _sub_order:
            for r in _by_sub[sub]:
                lines.append(
                    f"  {sub:<15} {r['ticker']:<6} {r.get('tier_role',''):<8} "
                    f"{r['within_weight']*100:>7.1f}% {r['portfolio_weight']*100:>6.1f}% "
                    f"{r['target_shares']:>7} ${r['price']:>8.2f}"
                )

    stock_trades = report.get("stock_trades", [])
    if stock_trades:
        capital = report.get("capital", 0)
        lines.append("")
        lines.append(f"  STOCK TRADES  (actual orders @ ${capital:,.0f})")
        lines.append("  " + "-" * 60)
        for tr in stock_trades:
            lines.append(
                f"  {tr['ticker']:<6} {tr['side']:<4} {tr['delta_shares']:>6} sh "
                f"@ ${tr['price']:>8.2f}  = ${tr['est_value']:>11,.0f}  "
                f"({tr['current_shares']}→{tr['target_shares']})"
            )

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
    force_signal_version: Optional[str] = None,
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
    config_path   : Path to config.yaml (default: semiconductor_strategy/config.yaml).

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

    # ── 0b. Corporate actions（拆股/合股）检测与应用 ────────────────
    # 必须在任何价格加载/MTM 之前：价格源（Polygon/yfinance）在 split 后
    # 全历史回溯调整，inventory 的 shares/cost_basis/last_price 仍是旧口径。
    # 与 mrpt/mtfs/ssrs 共用根目录 CorporateActions（统一 splits 源 + 留痕 +
    # 日志 trading_signals/corporate_actions.log）。幂等，失败降级不阻断。
    if not dry_run:
        try:
            from CorporateActions import run_for as _ca_run_for
            _ca = _ca_run_for('aiss')
            if _ca.get('applied'):
                log.warning(f"[CA][aiss] {len(_ca['applied'])} corporate action(s) "
                            f"applied to inventory — see corporate_actions.log")
        except Exception as _ca_e:
            log.warning(f"[CA][aiss] check failed (non-fatal): {_ca_e}")

    # ── 1. Load config ────────────────────────────────────────────
    cfg = load_config(config_path or CONFIG_PATH)
    if force_signal_version:
        cfg.setdefault("signals", {})["signal_version"] = force_signal_version
        log.info(f"[VERSION] Forced signal_version={force_signal_version} (overrides smart_select)")

    # ── 1a. 复利 sizing（ledger Phase 4）：capital = 账本真实 equity ────────
    # 利润按策略隔离复投（AISS 赚的归 AISS）。无账本/异常 → 回退名义 capital
    # 并响亮告警（宁名义勿假值）。equity 为上一交易日收盘 mark。
    try:
        from portfolio_ledger.ledger import Account as _LedgerAccount
        _acct = _LedgerAccount.load("aiss")
        if _acct and _acct.data.get("equity"):
            log.info(f"[LEDGER] compounding sizing: capital ${capital:,.0f} → "
                     f"account equity ${_acct.data['equity']:,.2f} "
                     f"(as_of {_acct.data['as_of']})")
            capital = float(_acct.data["equity"])
        else:
            log.warning("[LEDGER] account 无 equity — sizing 保持名义 capital")
    except Exception as _ledger_cap_e:
        log.warning(f"[LEDGER] compounding sizing 不可用（{_ledger_cap_e}）— 名义 capital")

    # ── 1b. Smart param select (P2) or static fallback ──────────
    _sel_path = CONFIG_PATH.parent / "selected_param_set.json"
    _smart_result = None  # will be set if smart_select succeeds
    _sel = {}
    if _sel_path.exists():
        _sel = json.loads(_sel_path.read_text())

    try:
        from semiconductor_strategy.AISSStrategyRuns import (
            PARAM_SETS as _PARAM_SETS,
            apply_param_set as _apply_param_set,
        )

        # Attempt smart param selection (P2) — needs cached batch data
        _smart_available = False
        try:
            from semiconductor_strategy.smart_select import smart_param_select, save_state
            # Load macro early for smart_select (will be reloaded properly in step 3)
            _macro_early = pd.DataFrame()
            try:
                _macro_early = load_macro(
                    start=cfg.get("data", {}).get("price_start", "2017-01-01"),
                    end=signal_date.strftime("%Y-%m-%d"),
                )
            except Exception:
                pass

            if not _macro_early.empty:
                _smart_result = smart_param_select(
                    signal_date=signal_date,
                    macro_df=_macro_early,
                    current_state=_sel,
                )
                _smart_available = _smart_result.get("smart_select_available", False)
        except Exception as _se:
            log.debug(f"[SMART SELECT] Unavailable ({_se}) — falling back to static JSON")

        if _smart_available and _smart_result:
            _ps_name = _smart_result["param_set"]
            _ps_ver = _smart_result["signal_version"]
            _switched = _smart_result.get("switched", False)

            if _ps_name and _ps_name in _PARAM_SETS:
                cfg = _apply_param_set(cfg, _PARAM_SETS[_ps_name])
                if _ps_ver and not force_signal_version:
                    cfg.setdefault("signals", {})["signal_version"] = _ps_ver

                _rank = _smart_result.get("current_rank", "?")
                _mcps = _smart_result.get("mcps_scores", {}).get(_ps_name, "?")
                _switch_msg = " [SWITCHED!]" if _switched else ""
                log.info(
                    f"[SMART SELECT] Active: {_ps_name} (ver={_ps_ver}) "
                    f"rank={_rank} mcps={_mcps}{_switch_msg}"
                )

                if _switched:
                    log.info(
                        f"[SMART SELECT] Switch: {_sel.get('param_set')} → {_ps_name} "
                        f"reason={_smart_result.get('switch_reason')}"
                    )

                # Persist updated state (dry-run 不写: 影子/周检不应推进防抖计数器
                # 与 selected_param_set.json —— 2026-07-21, E9 泄漏整改)
                if not dry_run:
                    try:
                        _smart_result["signal_date"] = signal_date
                        save_state(_sel, _smart_result)
                    except Exception:
                        pass
            else:
                log.warning(f"[SMART SELECT] Unknown param '{_ps_name}' — static fallback")
                _smart_available = False

        # Static fallback: read selected_param_set.json as before
        if not _smart_available and _sel:
            _ps_name = _sel.get("param_set")
            _ps_ver = _sel.get("signal_version")
            if _ps_name and _ps_name in _PARAM_SETS:
                cfg = _apply_param_set(cfg, _PARAM_SETS[_ps_name])
                if _ps_ver and not force_signal_version:
                    cfg.setdefault("signals", {})["signal_version"] = _ps_ver
                log.info(
                    f"[PARAM SELECT] Static fallback: {_ps_name} (ver={_ps_ver or 'v1'}) | "
                    f"selected={_sel.get('selected_at', '?')}"
                )
            elif _ps_name:
                log.warning(
                    f"[PARAM SELECT] Unknown param set '{_ps_name}' — using config defaults"
                )

    except Exception as _e:
        log.warning(f"[PARAM SELECT] Failed: {_e}")

    etf_tickers = cfg["universe"]["etfs"]           # e.g. ["XLE", "XLB", ...]
    benchmark   = cfg["universe"]["benchmark"]      # "SPY"
    port_cfg    = cfg.get("portfolio", {})
    reb_cfg     = cfg.get("rebalance", {})
    risk_cfg    = cfg.get("risk", {})
    cost_cfg    = cfg.get("costs", {})
    sig_cfg     = cfg.get("signals", {})

    end_date_str = signal_date.strftime("%Y-%m-%d")

    # ── 2. Load prices ─────────────────────────────────────────────
    # The price store auto-refreshes stale tickers to ``end`` inside the loader
    # (data/aiss_fetch_prices.load_prices_wide), so marks are current without any
    # explicit refresh here — same pattern as SSRS loader.load_prices.
    log.info("Loading ETF prices...")
    prices_all = load_etf_prices(etf_tickers, benchmark, end=end_date_str)
    etf_prices = prices_all[[t for t in etf_tickers if t in prices_all.columns]]
    bench_prices = prices_all[[benchmark]] if benchmark in prices_all.columns else None

    # Prices as of signal_date
    prices_today = prices_all.iloc[-1]  # last available row

    # ── 2b. Individual-stock prices (for the stock-decomposition layer) ──
    # AISS's tradeable "asset" is the subsector basket; the executable layer is
    # one level below — the real single stocks.  Load their prices + first-trade
    # dates so the daily signal can decompose subsector weights into stock orders.
    try:
        _stock_universe = all_tickers(include_benchmark=False)
        stock_prices_all = load_stock_prices(_stock_universe, start=PRICE_START, end=end_date_str)
        stock_prices_today = stock_prices_all.iloc[-1] if not stock_prices_all.empty else pd.Series(dtype=float)
        stock_first_avail = {t: stock_prices_all[t].first_valid_index() for t in stock_prices_all.columns}
    except Exception as _sp_e:
        log.warning(f"Stock-price load failed ({_sp_e}); stock decomposition will be skipped.")
        stock_prices_all = pd.DataFrame()
        stock_prices_today = pd.Series(dtype=float)
        stock_first_avail = {}

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
    from semiconductor_strategy.signals.composite import DEFAULT_REGIME_WEIGHT_MULTIPLIERS
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
        "signal_version": sig_cfg.get("signal_version", "v1"),
        "stm_enabled": stm_cfg.get("enabled", False),
        "stm_lookback": stm_cfg.get("lookback_months", 6),
        "stm_skip": stm_cfg.get("skip_months", 1),
        "stm_zscore_window": stm_cfg.get("zscore_window", 24),
        "erm_enabled": erm_cfg.get("enabled", False),
        "erm_lookback_quarters": erm_cfg.get("lookback_quarters", 4),
        "rsb_enabled": rsb_cfg.get("enabled", False),
        "rsb_lookback_days": rsb_cfg.get("lookback_days", 63),
        "use_external_macro": sig_cfg.get("supply_chain", {}).get("use_external_macro", True),
        "supply_chain": sig_cfg.get("supply_chain", {}),
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

    # ── 5b. Macro-conditioned weight tilt (P3) ───────────────────
    if _smart_result and _smart_result.get("smart_select_available"):
        try:
            from semiconductor_strategy.smart_select import macro_weight_tilt
            target_weights_raw = macro_weight_tilt(
                target_weights_raw, macro, signal_date, max_tilt=0.05)
            log.info("[P3] Applied macro-conditioned weight tilt (±5%)")
        except Exception as _tilt_e:
            log.debug(f"[P3] Weight tilt skipped: {_tilt_e}")

    # Inventory loaded here (early) so the event-risk overlay (6a) can read/persist
    # its event_derisk_active state before risk controls are applied.
    inv = load_inventory()

    # ── 6. Apply risk controls ─────────────────────────────────────
    log.info("Applying risk controls...")
    # Approximate portfolio returns: equal-weight sector basket
    portfolio_returns = daily_returns.mean(axis=1)

    prog_cfg   = risk_cfg.get("vix_progressive_derisk", {})
    prog_tiers = prog_cfg.get("tiers", []) if prog_cfg.get("enabled", False) else []

    # ── 6a. Event-risk overlay (semi de-risk; default off) ──────────
    # Independent detector (shared EventRiskDetector); mirrors emergency_mode_active
    # lifecycle (persist in inventory; daily re-apply holds the de-risk, no buy-back).
    ev_cfg = risk_cfg.get("event_derisk", {})
    event_active = False
    event_reason = ""
    if ev_cfg.get("enabled", False):
        try:
            import sys as _sys, os as _os
            _repo = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
            if _repo not in _sys.path:
                _sys.path.insert(0, _repo)
            import EventRiskDetector as _erd
            from semiconductor_strategy.portfolio.risk import estimate_sector_betas as _esb
            # AISS portfolio beta vs SPY on the RAW (pre-de-risk) target weights (§3)
            _aiss_beta = float("nan")
            _bench_ret = (bench_prices[benchmark].pct_change().dropna()
                          if bench_prices is not None and benchmark in bench_prices.columns else None)
            if _bench_ret is not None and not daily_returns.empty:
                _sb = _esb(daily_returns, _bench_ret)
                _aiss_beta = float((target_weights_raw *
                                    _sb.reindex(target_weights_raw.index).fillna(1.0)).sum())
            _prev = {"active": inv.get("event_derisk_active", False),
                     "signal_date": inv.get("event_signal_date"),
                     "reason": inv.get("event_derisk_reason", ""),
                     "reduce_done": inv.get("event_reduce_done", False)}
            _state, _ev = _erd.process(
                signal_date, _prev, aiss_beta=_aiss_beta,
                beta_mode=ev_cfg.get("beta_mode", "bottomup"),
                beta_threshold=ev_cfg.get("beta_threshold", 2.5),
                nfp_days=ev_cfg.get("nfp_window_days", 2),
                bellwether_thresh=ev_cfg.get("bellwether_drop", -0.045))
            event_active = bool(_state.get("veto_next_open"))
            event_reason = _state.get("reason", "")
            inv["event_derisk_active"] = bool(_state.get("active"))
            inv["event_signal_date"]   = _state.get("signal_date")
            inv["event_derisk_reason"] = event_reason
            inv["event_reduce_done"]   = bool(_state.get("reduce_done"))
            log.info(f"[EVENT_DERISK] hit={_ev['hit']} beta_used={_ev.get('beta_used')} "
                     f"active_next={event_active} triggers={_ev['triggers']}")
            # Daily heartbeat (written every run, even when nothing triggers)
            _erd.log_evaluation(_ev, _state, "AISS",
                                str(SIGNALS_DIR / "event_risk_heartbeat.log"),
                                extra=f"aiss_beta={_aiss_beta:.2f}")
        except Exception as _e:  # noqa: BLE001
            log.warning(f"[EVENT_DERISK] skipped (non-fatal): {_e}")

    target_weights, cash_weight, risk_flags = apply_risk_controls(
        weights=target_weights_raw,
        portfolio_returns=portfolio_returns,
        macro=macro_recent,
        # 实盘 DD 断路器(2026-07-22): 账本真实 equity 序列(回测-实盘不对称修复)
        equity_curve=(_load_ledger_equity_curve()
                      if risk_cfg.get("drawdown", {}).get("live_dd_enabled", True) else None),
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
        vol_downside_only=risk_cfg.get("vol_scaling", {}).get("downside_only", False),
        vix_emergency_threshold=reb_cfg.get("emergency_derisk_vix", 35.0),
        emergency_cash_pct=reb_cfg.get("emergency_cash_pct", 0.50),
        dd_halve_threshold=risk_cfg.get("drawdown", {}).get("cumulative_dd_halve", -0.15),
        dd_recovery_threshold=risk_cfg.get("drawdown", {}).get("cumulative_dd_recovery", -0.10),
        dd_release_rebound=risk_cfg.get("drawdown", {}).get("recovery_release_rebound", 0.0),
        max_weight=port_cfg.get("constraints", {}).get("max_weight", 0.55),
        beta_min=port_cfg.get("constraints", {}).get("beta_min", 0.40),
        beta_max=port_cfg.get("constraints", {}).get("beta_max", 3.00),
        vix_progressive_tiers=prog_tiers,
        event_derisk_active=event_active,
        event_derisk_frac=ev_cfg.get("sell_frac", 0.5),
        event_derisk_reason=event_reason,
    )

    # ── 6b. Macro anomaly auto-conservative (P3) ────────────────
    if (_smart_result and _smart_result.get("macro_positioning", {}).get("anomaly")):
        # Novel macro environment → reduce all positions by 20%
        _anomaly_scale = 0.80
        target_weights = target_weights * _anomaly_scale
        cash_weight = 1.0 - float(target_weights.sum())
        risk_flags = str(risk_flags) + " | macro_anomaly_conservative"
        log.warning(
            f"[P3] Macro anomaly detected — positions reduced by 20%. "
            f"Cluster dist={_smart_result['macro_positioning'].get('cluster_distance')}"
        )

    log.info(f"Target weights (post-risk):\n{target_weights.round(3).to_string()}")
    log.info(f"Cash allocation: {cash_weight:.1%}  Risk flags: {risk_flags}")

    # ── 7. Rebalance decision ──────────────────────────────────────
    # (inv already loaded above at step 6 for the event-risk overlay)
    inv["capital"] = capital
    # Whether the per-calendar-day update already ran today — captured ONCE here,
    # before any branch mutates inv["last_daily_update"], so the subsector AND the
    # stock-level days_held increments use the same flag.  (Bug fix: the stock block
    # used to recompute this AFTER the subsector branch had set last_daily_update,
    # so _already was always True there → stock days_held never incremented on HOLD.)
    _already_today = (inv.get("last_daily_update") == signal_date.isoformat())

    # P5: If smart-select switched the signal VERSION *or* the PARAM SET, force a
    # full rebalance and clear prev_composite_scores.  Clearing prev scores makes
    # the rebalance bypass the zscore-threshold filter (which keeps prior weights
    # when per-subsector scores are stable) so the NEW param's optimizer weights
    # are actually adopted.  Without this, a param/optimizer switch (e.g.
    # opt_equal_weight → balanced_four) would leave the book stuck on the old
    # allocation whenever signals move < threshold.  (inv["param_set"]/
    # ["signal_version"] still hold the PREVIOUS run's values at this point — set
    # at end of run — so this compares new-vs-previous.)
    if _smart_result and _smart_result.get("switched"):
        _new_ver   = _smart_result.get("signal_version")
        _new_param = _smart_result.get("param_set")
        _ver_switch   = bool(_new_ver) and _new_ver != inv.get("signal_version")
        _param_switch = bool(_new_param) and _new_param != inv.get("param_set")
        if _ver_switch or _param_switch:
            inv["prev_composite_scores"] = {}   # bypass threshold filter → adopt new weights
            force_rebalance = True
            if _new_ver:
                inv["signal_version"] = _new_ver
            _what = []
            if _ver_switch:   _what.append(f"version→{_new_ver}")
            if _param_switch: _what.append(f"param→{_new_param}")
            log.info(
                f"[P5] Switch detected ({', '.join(_what)}) → clearing "
                f"prev_composite_scores, forcing full rebalance"
            )

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
        # 防线 A：risk_flags 在 macro-anomaly 分支可能被覆盖成 str → getattr 兜底 False
        vol_derisk_ctx={
            "triggered": bool(getattr(risk_flags, "vol_scaling_triggered", False)),
            "target_cash": float(cash_weight),
        },
    )
    # Persist emergency state: set True on trigger, False on recovery or monthly rebalance
    if rebalance_reason == "emergency_vix":
        emergency_active = True
    elif rebalance_reason == "monthly_rebalance":
        emergency_active = False   # Monthly rebalance resets emergency mode
    inv["emergency_mode_active"] = emergency_active

    # ── 防线 A：vol_derisk streak 幂等维护（同日重跑不重复计数，与 last_daily_update 同模式）──
    if str(inv.get("vol_derisk_streak_date") or "") != str(signal_date):
        if will_rebalance and rebalance_reason == "vol_derisk":
            inv["vol_derisk_streak"] = 0        # 触发执行 → 清零重新累计
        else:
            _vs_trig = bool(getattr(risk_flags, "vol_scaling_triggered", False))
            inv["vol_derisk_streak"] = (int(inv.get("vol_derisk_streak", 0) or 0) + 1) if _vs_trig else 0
        inv["vol_derisk_streak_date"] = str(signal_date)
    if will_rebalance and rebalance_reason == "vol_derisk":
        log.warning(f"[VOL_DERISK] interim de-risk rebalance triggered: "
                    f"target_cash={float(cash_weight):.1%} current_cash={float(inv.get('cash_weight', 0) or 0):.1%}")

    log.info(f"Rebalance: {will_rebalance}  reason={rebalance_reason}")

    # Current holdings from inventory
    current_weights = pd.Series(
        {t: d.get("weight", 0.0) for t, d in inv.get("holdings", {}).items()},
        dtype=float,
    )
    current_shares: Dict[str, int] = {
        t: int(d.get("shares", 0)) for t, d in inv.get("holdings", {}).items()
    }
    # Previous stock-level holdings (for stock-order deltas) — captured BEFORE
    # the inventory is overwritten below.
    prev_stock_holdings: Dict[str, dict] = dict(inv.get("stock_holdings", {}) or {})

    # If no rebalance today: keep current weights, only update prices
    if not will_rebalance:
        effective_weights = current_weights if not current_weights.empty else target_weights
        effective_shares  = current_shares
        actions = {t: ACTION_HOLD for t in effective_weights.index}
    else:
        # Apply zscore threshold filter (only rebalance sectors with significant change)
        from semiconductor_strategy.portfolio.rebalance import apply_zscore_threshold_filter
        prev_scores = pd.Series(inv.get("prev_composite_scores", {}), dtype=float)
        # Skip the threshold filter when: first run / no prev scores (all new), OR a
        # force-rebalance was requested.  A forced rebalance means "adopt the current
        # optimizer target now" — so it bypasses the stability filter that would
        # otherwise keep prior weights on small score moves (which silently froze
        # forced rebalances, e.g. applying a new supply-chain graph version).
        # NOTE: the monthly pipeline mode also passes --force-rebalance, so monthly
        # now fully realigns to the target each month (turnover slightly higher) by
        # design, rather than being damped by the threshold filter.
        if prev_scores.empty or force_rebalance:
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
        from semiconductor_strategy.portfolio.rebalance import cap_turnover
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

    # ── 8b. Stock-decomposition layer (one level below subsectors) ──
    # SSRS trades 11 ETFs directly; AISS's subsector is a synthetic basket, so the
    # executable layer is the underlying single stocks.  Decompose the final
    # subsector weights into PIT-correct per-stock holdings + orders.
    _held_subsector_w = {
        t: float(effective_weights.get(t, 0.0))
        for t in effective_weights.index
        if float(effective_weights.get(t, 0.0)) > 0
    }
    # Accident detection: any universe stock with no valid price in the trailing
    # 10 trading days (halt/delisting/data outage) → its subsector's weight routes
    # to the 0%-reserve inside effective_weights.  Normally empty → 3-stock baskets.
    _unavailable = recently_unavailable(stock_prices_all, signal_date, stale_days=10) \
        if not stock_prices_all.empty else set()
    if _unavailable:
        log.warning("Stale/unavailable stocks (reserve will be promoted): %s", sorted(_unavailable))
    stock_decomp = decompose_to_stocks(
        subsector_weights=_held_subsector_w,
        capital=capital,
        as_of_date=signal_date,
        stock_prices_today=stock_prices_today,
        first_available=stock_first_avail,
        min_history_months=int(cfg.get("universe", {}).get("min_history_months", 24)),
        unavailable=_unavailable,
    )
    stock_trades = build_stock_trades(stock_decomp["by_ticker"], prev_stock_holdings,
                                      prices_today=stock_prices_today) if will_rebalance else []
    log.info(
        f"Stock layer: {len(stock_decomp['by_ticker'])} stocks across "
        f"{len(_held_subsector_w)} subsectors; {len(stock_trades)} stock trades"
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
            force=force_rebalance,
        )
    else:
        # Non-rebalance day: update last_price + increment days_held (idempotent via last_daily_update)
        today_str = signal_date.isoformat()
        already_updated = _already_today
        for t, holding in inv.get("holdings", {}).items():
            p = float(prices_today.get(t, holding.get("last_price", 0.0)))
            if p > 0:
                holding["last_price"] = round(p, 4)
            if not already_updated:
                holding["days_held"] = holding.get("days_held", 0) + 1
        if not already_updated:
            inv["as_of"] = today_str
            inv["last_daily_update"] = today_str

    # 子板块簿记重锚定到当前指数 vintage（拆股回溯调整免疫；两条路径都走）
    _reanchor_subsector_holdings(inv.get("holdings", {}), prices_all, capital)

    # Record the active param set + signal version on the inventory (auditability:
    # which config generated these positions). smart_select pick > static
    # selected_param_set > "default". signal_version reflects the resolved cfg.
    inv["param_set"] = (
        (_smart_result.get("param_set") if _smart_result else None)
        or _sel.get("param_set") or "default"
    )
    inv["signal_version"] = cfg.get("signals", {}).get("signal_version", "v1")

    # Stock-level holdings (executable layer below subsectors). On rebalance,
    # store the fresh decomposition; otherwise keep prior shares and refresh price.
    if will_rebalance:
        inv["stock_holdings"] = stock_holdings_from_by_ticker(stock_decomp["by_ticker"])
    else:
        _sh = dict(prev_stock_holdings)
        for _tk, _h in _sh.items():
            _p = float(stock_prices_today.get(_tk, _h.get("last_price", 0.0))) if not stock_prices_today.empty else _h.get("last_price", 0.0)
            if _p and _p > 0:
                _h["last_price"] = round(_p, 4)
        inv["stock_holdings"] = _sh

    # Per-STOCK cost basis / entry date / days held (executable layer). Mirrors the
    # subsector accounting: ENTER resets, otherwise carry forward. Legacy positions
    # (opened before this field existed) are backfilled — cost basis = the stock's
    # price on its subsector's entry date (from history), entry/days inherited from
    # the subsector. The displayed current price stays the live per-stock last_price.
    _today_str = signal_date.isoformat()
    _already = _already_today   # captured before last_daily_update was set this run
    _sub_holdings = inv.get("holdings", {}) or {}
    for _tk, _h in inv.get("stock_holdings", {}).items():
        _sub = (_h.get("subsectors") or [None])[0]
        _act = actions.get(_sub, ACTION_HOLD) if _sub else ACTION_HOLD
        _prev = prev_stock_holdings.get(_tk, {})
        _px = float(_h.get("last_price", 0.0) or 0.0)
        if will_rebalance and _act == ACTION_ENTER \
                and not (_prev.get("shares") and _prev.get("cost_basis") is not None):
            # 真正的新建仓才重置成本。股票昨天已持有、只是换了子板块归属
            # （ARM 6/30 ai_gpu → 7/1 logic_cpu）时走下一分支继承成本/入场日/
            # 天数——仓位级历史不因子板块换血而断裂。
            _h["cost_basis"] = round(_px, 4)
            _h["entry_date"] = _today_str
            _h["days_held"]  = 1
        elif _prev.get("cost_basis") is not None:
            _h["cost_basis"] = _prev["cost_basis"]
            _h["entry_date"] = _prev.get("entry_date", _today_str)
            # increment once per calendar day (idempotent on same-day re-runs)
            _h["days_held"]  = _prev.get("days_held", 0) + (0 if _already else 1)
            # Carry corporate-actions provenance through rebalance rewrites.
            # Without it, a rewritten holding keeps a pre-split entry_date but
            # loses the "already adjusted" marker → adjust_stock_holding_view
            # re-applies the split (KLAC 465→4650 shares, fake +32% on 7/1).
            if _prev.get("applied_corporate_actions"):
                _h["applied_corporate_actions"] = _prev["applied_corporate_actions"]
        else:
            # backfill legacy: inherit subsector entry/days; cost = price on entry date
            _subh = _sub_holdings.get(_sub, {}) if _sub else {}
            _ed = _subh.get("entry_date", _today_str)
            _cb = _px
            try:
                if _tk in stock_prices_all.columns:
                    _col = stock_prices_all[_tk].dropna()
                    _asof = _col[_col.index <= pd.Timestamp(_ed)]
                    if len(_asof):
                        _cb = float(_asof.iloc[-1])
            except Exception:
                pass
            _h["cost_basis"] = round(_cb, 4)
            _h["entry_date"] = _ed
            _h["days_held"]  = int(_subh.get("days_held", 1))
        _h["action_today"] = _act

    save_inventory(inv, dry_run=dry_run)

    # ── 10b. Ledger 记账 + 当日 PnL/Risk 报告（非致命；报告需 reportlab →
    #        由 someopark_run 子进程生成，本进程只做纯 pandas 记账）──────────
    if not dry_run:
        try:
            from portfolio_ledger.ledger import daily_update as _ledger_update
            if _ledger_update("aiss") > 0:
                # 同步执行（勿改回 Popen fire-and-forget：cron 包装器在主进程
                # 退出时回收进程组，子进程在 conda 启动阶段就被杀 —— 2026-07-02
                # AISS 首战报告因此丢失）。
                import subprocess
                _rp = subprocess.run(
                    ["conda", "run", "-n", "someopark_run", "python", "-m",
                     "portfolio_ledger.reports", "aiss"],
                    cwd=str(Path(__file__).resolve().parent.parent),
                    capture_output=True, timeout=300)
                if _rp.returncode == 0:
                    log.info("[ledger] 当日 PnL/Risk 报告已生成")
                else:
                    log.warning(f"[ledger] 报告生成失败 rc={_rp.returncode}: "
                                f"{_rp.stderr.decode()[-200:]}")
        except Exception as _ledger_e:
            log.warning(f"[ledger] non-fatal: {_ledger_e}")

    # ── 11. Get macro snapshot for report ─────────────────────────
    macro_last = macro_recent.iloc[-1] if not macro_recent.empty else pd.Series(dtype=float)

    # ── 12. Assemble full report ───────────────────────────────────
    # ── Full tradable universe: ALL 8 subsectors × (3 weighted tiers + 0%-reserve
    #    4th stock), including UNSELECTED subsectors at 0%, for the frontend
    #    "Tradable Universe" view. Stock weight = subsector_weight × within-tier weight. ──
    from semiconductor_strategy.data import universe as _UNIV
    _TIER_ROLES = ["primary", "backup1", "backup2"]
    stock_universe = []
    for _sub in _UNIV.subsector_names():
        _subw = float(effective_weights.get(_sub, 0.0))
        _within = _UNIV.subsector_weights(_sub)        # {ticker: within_weight} (3 tiers, 0.8/0.15/0.05)
        _stocks = []
        for _i, (_tk, _w) in enumerate(_within.items()):
            _px = float(stock_prices_today.get(_tk, 0.0)) if hasattr(stock_prices_today, "get") else 0.0
            _stocks.append({
                "ticker": _tk,
                "tier_role": _TIER_ROLES[_i] if _i < len(_TIER_ROLES) else f"tier{_i}",
                "within_weight": round(_w, 6),
                "portfolio_weight": round(_subw * _w, 6),
                "price": round(_px, 2),
            })
        _res = _UNIV.subsector_reserve(_sub)            # 0%-reserve 4th stock (promoted only on accident)
        if _res:
            _pxr = float(stock_prices_today.get(_res, 0.0)) if hasattr(stock_prices_today, "get") else 0.0
            _stocks.append({
                "ticker": _res, "tier_role": "reserve",
                "within_weight": 0.0, "portfolio_weight": 0.0, "price": round(_pxr, 2),
            })
        stock_universe.append({
            "subsector":        _sub,
            "display":          _UNIV.subsector_display(_sub),
            "subsector_weight": round(_subw, 6),
            "held":             _subw > 0.001,
            "composite_score":  round(float(scores_today.get(_sub, float("nan"))), 4) if hasattr(scores_today, "get") else None,
            "stocks":           _stocks,
        })

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
        "stock_holdings":      stock_decomp["by_ticker"],   # executable per-stock layer
        "stock_breakdown":     stock_decomp["breakdown"],   # per (subsector, stock)
        "stock_universe":      stock_universe,              # ALL 8 subsectors × 4 stocks (incl unselected/reserve 0%)
        "stock_trades":        stock_trades,                # per-stock orders (rebalance)
        "holdings_summary": {
            "n_positions": sum(1 for s in signal_list if s["target_weight"] > 0),
            "invested_pct": round(sum(s["target_weight"] for s in signal_list) * 100, 2),
            "cash_pct":     round(cash_weight * 100, 2),
        },
    }

    # Add smart-select metadata to report (P2/P3/P5)
    if _smart_result and _smart_result.get("smart_select_available"):
        report["smart_select"] = {
            "param_set": _smart_result.get("param_set"),
            "signal_version": _smart_result.get("signal_version"),
            "switched": _smart_result.get("switched", False),
            "switch_reason": _smart_result.get("switch_reason"),
            "current_rank": _smart_result.get("current_rank"),
            "mcps_score": _smart_result.get("mcps_scores", {}).get(
                _smart_result.get("param_set")),
            "best_candidate": _smart_result.get("best_candidate"),
            "version_selector": _smart_result.get("version_selector"),
            "anomaly_detected": _smart_result.get("macro_positioning", {}).get("anomaly"),
            "nearest_cluster": _smart_result.get("macro_positioning", {}).get("nearest_cluster"),
        }

    # ── 13. Write reports ──────────────────────────────────────────
    _write_report_json(report, signal_date)
    _write_report_txt(report, signal_date)

    # ── 14. Monitor Excel (on rebalance days only) ────────────────
    if will_rebalance and not dry_run:
        try:
            from semiconductor_strategy.portfolio_record import SectorRotationRecord
            _ps_name = _smart_result.get("param_set", "") if _smart_result else \
                       _sel.get("param_set", "")
            _ps_ver = cfg.get("signals", {}).get("signal_version", "v1")
            _rec = SectorRotationRecord(
                result=type("R", (), {"risk_flags": [], "signals_history": pd.DataFrame(),
                                      "regime_history": pd.Series(), "config": cfg,
                                      "weights_history": pd.DataFrame(), "equity_curve": pd.Series(),
                                      "daily_returns": pd.Series(), "metrics": {},
                                      "stop_loss_events": None, "position_states_history": None})(),
                prices=prices_all,
                macro=macro,
                param_set=_ps_name,
                signal_version=_ps_ver,
            )
            _rec.export_monitor_excel(signal_date, report)
        except Exception as _mon_e:
            log.debug(f"[MONITOR] Excel export skipped: {_mon_e}")

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
        description="AISS Daily Signal Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard daily run (proxy P/E, fast)
  conda run -n qlib_run python semiconductor_strategy/AISSdailySignal.py --capital 1000000

  # With real constituent P/E (slow on first run, cached afterwards)
  conda run -n qlib_run python semiconductor_strategy/AISSdailySignal.py \\
    --capital 1000000 --value-source constituents

  # Dry run for today
  conda run -n qlib_run python semiconductor_strategy/AISSdailySignal.py --dry-run

  # Force rebalance on a specific date
  conda run -n qlib_run python semiconductor_strategy/AISSdailySignal.py \\
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
        help="Force a full rebalance now: bypasses the schedule, the zscore "
             "threshold filter (adopts the optimizer target), AND the same-day "
             "idempotency guard. Use to apply a new graph/param immediately.",
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
        help="Path to config.yaml (default: semiconductor_strategy/config.yaml)",
    )
    parser.add_argument(
        "--signal-version", choices=["v1", "v2"], default=None,
        help="Force V1 (monthly) or V2 (semi-monthly) rebalance, overriding smart_select.",
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
        force_signal_version=args.signal_version,
    )

    # Print summary to stdout
    if report:
        print()
        print("=" * 64)
        print(f"  AISS  —  {sig_date}  (dry_run={args.dry_run})")
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
