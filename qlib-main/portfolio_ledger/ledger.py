"""记账引擎：账户状态、交易/分红/费用入账、口径归一、每日处理。

所有金额 USD；所有股数/成本一律**当前口径**（current caliber）——历史记录读入时
按 splits_cache 归一（execution_date > 记录日期 且 ≤ 今日 的拆股连乘），与
Polygon store 的回溯调整价同口径。这样 replay 全程无拆股跳变，无需在账内
处理 split 事件（等价变换，价值不变：127股@$1940 ≡ 1270股@$194）。
"""
from __future__ import annotations

import glob
import json
import logging
import os
from datetime import date as _date

import pandas as pd

log = logging.getLogger("portfolio_ledger")

_THIS = os.path.dirname(os.path.abspath(__file__))
QLIB_DIR = os.path.dirname(_THIS)
BASE_DIR = os.path.dirname(QLIB_DIR)                      # repo root
PRICE_DATA = os.path.join(BASE_DIR, "price_data")
SPLITS_CACHE = os.path.join(PRICE_DATA, "splits_cache.json")
DIVIDENDS_CACHE = os.path.join(PRICE_DATA, "dividends_cache.json")

INITIAL_CASH = 1_000_000.0

# ── 策略配置 ────────────────────────────────────────────────────────────────
STRATEGIES = {
    "aeus": {
        "dir": os.path.join(QLIB_DIR, "electric_utilities_strategy"),
        "snap_glob": "inventory_history/inventory_aeus_*.json",
        "holdings_key": "stock_holdings",
        "store_dir": os.path.join(PRICE_DATA, "elec_strategy", "prices"),
        "live_start": "2026-09-01",          # = UpdateMasterPerformance.AEUS_LIVE_START
        "report_glob": "trading_signals/aeus_daily_report_*.json",
        "benchmarks": ["XLU", "GRID", "SPY"],
    },
    "aiss": {
        "dir": os.path.join(QLIB_DIR, "semiconductor_strategy"),
        "snap_glob": "inventory_history/inventory_aiss_*.json",
        "holdings_key": "stock_holdings",
        "store_dir": os.path.join(PRICE_DATA, "semi_strategy", "prices"),
        "live_start": "2026-06-01",          # = UpdateMasterPerformance.AISS_LIVE_START
        "report_glob": "trading_signals/aiss_daily_report_*.json",
        "benchmarks": ["SMH", "SPY"],
    },
    "ssrs": {
        "dir": os.path.join(QLIB_DIR, "sector_rotation"),
        "snap_glob": "inventory_history/inventory_sector_rotation_*.json",
        "holdings_key": "holdings",
        "store_dir": os.path.join(PRICE_DATA, "sector_etfs", "polygon"),
        # 账本起点（用户指定 2026-05-01 = 首次月度调仓日；4/27 为初始建仓/
        # 孵化期不计入）。5/1 快照（调仓后持仓@成本）作 opening balance。
        # 注意 ≠ UpdateMasterPerformance.SR_LIVE_START(5/8)——那是 master 曲线的
        # 回测拼接点，不是账本起点。
        "live_start": "2026-05-01",
        "report_glob": "trading_signals/sr_daily_report_*.json",
        "benchmarks": ["SPY"],
    },
}


def _cfg(strategy: str) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    return STRATEGIES[strategy]


def account_path(strategy: str) -> str:
    return os.path.join(_cfg(strategy)["dir"], f"account_{strategy}.json")


def history_dir(strategy: str) -> str:
    return os.path.join(_cfg(strategy)["dir"], "account_history")


def ledger_path(strategy: str) -> str:
    return os.path.join(_cfg(strategy)["dir"], f"trade_ledger_{strategy}.jsonl")


# ── 拆股 / 分红（口径归一基础设施）──────────────────────────────────────────

def load_splits_by_ticker(today_str: str | None = None) -> dict:
    """splits_cache → {ticker: [split, ...]}，只留 execution_date ≤ 今日的
    （未来生效的拆股 store 还没调整，绝不能用——cache 里确有 7/20+ 的未来条目）。"""
    today_str = today_str or str(_date.today())
    out: dict = {}
    try:
        with open(SPLITS_CACHE) as f:
            cache = json.load(f)
    except Exception:
        return out
    for sp in cache.get("results", []):
        ed = sp.get("execution_date", "")
        if ed and ed <= today_str:
            out.setdefault(sp.get("ticker"), []).append(sp)
    return out


def caliber_factor(ticker: str, record_date: str, splits_by_ticker: dict) -> float:
    """记录日期 → 当前口径的股数乘数：Π(to/from) over splits 满足
    execution_date > record_date。成本/每股金额除以同一因子。"""
    f = 1.0
    for sp in splits_by_ticker.get(ticker, []):
        if sp.get("execution_date", "") > record_date:
            try:
                f *= float(sp["split_to"]) / float(sp["split_from"])
            except Exception:
                continue
    return f


def load_dividends_by_ticker(tickers: list, start: str, end: str,
                             api_key: str | None = None) -> dict:
    """{ticker: [{ex_dividend_date, cash_amount}, ...]}，只留 ex∈[start,end]。

    缓存优先（dividends_cache.json，与 PriceDataStore._fetch_dividends 同一文件
    同一 schema）；缺失 symbol 且有 POLYGON_API_KEY 时拉 Polygon 并回写缓存。"""
    try:
        with open(DIVIDENDS_CACHE) as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    api_key = api_key or os.environ.get("POLYGON_API_KEY")
    dirty = False
    out: dict = {}
    for t in tickers:
        entry = cache.get(t)
        if entry is None or entry.get("fetched_through", "") < end:
            if api_key:
                fetched = _fetch_dividends_polygon(t, api_key)
                if fetched is not None:
                    entry = {"fetched_through": str(_date.today()), "dividends": fetched}
                    cache[t] = entry
                    dirty = True
        divs = (entry or {}).get("dividends", [])
        sel = [d for d in divs if start <= d.get("ex_dividend_date", "") <= end]
        if sel:
            out[t] = sorted(sel, key=lambda d: d["ex_dividend_date"])
    if dirty:
        try:
            with open(DIVIDENDS_CACHE, "w") as f:
                json.dump(cache, f)
        except Exception as e:
            log.warning(f"dividends_cache write failed: {e}")
    return out


def _fetch_dividends_polygon(symbol: str, api_key: str) -> list | None:
    """全量拉取一个 symbol 的分红（分页），schema 与 PriceDataStore 缓存一致。"""
    import requests
    url = (f"https://api.polygon.io/v3/reference/dividends"
           f"?ticker={symbol}&limit=1000&apiKey={api_key}")
    all_divs: list = []
    try:
        while url:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            j = r.json()
            all_divs.extend(j.get("results", []))
            nxt = j.get("next_url")
            url = f"{nxt}&apiKey={api_key}" if nxt else None
        return all_divs
    except Exception as e:
        log.warning(f"polygon dividends fetch failed for {symbol}: {e}")
        return None


# ── 价格（Polygon store only，禁 yfinance）──────────────────────────────────

def load_store_prices(strategy: str, tickers: list,
                      field: str = "AdjClose") -> pd.DataFrame:
    """从策略自己的 Polygon parquet store 读价格宽表。
    缺 ticker = 硬告警（宁缺毋假，不做任何兜底价格源）。

    field="Close" 供**收盘 mark** 专用（2026-08-20）：账户是真金白银的盯市，
    必须用真实收盘价。AdjClose 是总收益序列，用它 mark 会把股息记两遍——
    除权日 AdjClose 自动抬回 Close（+d×shares 的 unrealized 跳变），而
    process_day 同日又把同一笔股息入了现金。实测 SSRS 6/19 的 $4,837.86
    被计两次 = 发布曲线永久虚高 42.82bp。AdjClose 只该喂因子/回测。"""
    store = _cfg(strategy)["store_dir"]
    cols = {}
    for t in sorted(set(tickers)):
        fp = os.path.join(store, f"{t}_prices.parquet")
        if not os.path.exists(fp):
            log.error(f"[{strategy}] POLYGON STORE MISSING {t} ({fp}) — ticker skipped")
            continue
        df = pd.read_parquet(fp)
        col = field if field in df.columns else "Close"
        cols[t] = df[col]
    if not cols:
        raise RuntimeError(f"[{strategy}] no prices loaded from {store}")
    px = pd.DataFrame(cols)
    px.index = pd.DatetimeIndex(px.index).normalize()
    return px.sort_index()


# ── 快照读取（含口径归一）────────────────────────────────────────────────────

def load_snapshots(strategy: str, splits_by_ticker: dict) -> dict:
    """{as_of: {ticker: {shares, cost_basis}}}，同日多快照取文件名最新。
    股数/成本按 splits 归一到当前口径；带 applied_corporate_actions 留痕的
    记录里已应用的 split 不重复归一（双保险，镜像守卫 1）。"""
    cfg = _cfg(strategy)
    key = cfg["holdings_key"]
    snaps: dict = {}
    for fp in sorted(glob.glob(os.path.join(cfg["dir"], cfg["snap_glob"]))):
        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception:
            continue
        as_of = data.get("as_of", "")
        if not as_of:
            continue
        holdings = {}
        for t, h in (data.get(key) or {}).items():
            if not isinstance(h, dict):
                continue
            shares = h.get("shares", 0) or 0
            cost = h.get("cost_basis", h.get("last_price", 0)) or 0
            applied = {a.get("polygon_id") for a in h.get("applied_corporate_actions", [])
                       if isinstance(a, dict)}
            f_total = 1.0
            for sp in splits_by_ticker.get(t, []):
                if sp.get("execution_date", "") > as_of and sp.get("id") not in applied:
                    try:
                        f_total *= float(sp["split_to"]) / float(sp["split_from"])
                    except Exception:
                        continue
            if f_total != 1.0:
                shares = int(round(shares * f_total))
                cost = cost / f_total
            holdings[t] = {"shares": int(shares), "cost_basis": float(cost),
                           "entry_date": h.get("entry_date", as_of)}
        snaps[as_of] = holdings          # 同日多文件：sorted 顺序下最后一个覆盖
    return snaps


def load_fees_by_date(strategy: str) -> dict:
    """{signal_date: total_cost_usd} 来自 daily report 的 transaction_costs。"""
    cfg = _cfg(strategy)
    out: dict = {}
    for fp in sorted(glob.glob(os.path.join(cfg["dir"], cfg["report_glob"]))):
        try:
            with open(fp) as f:
                r = json.load(f)
        except Exception:
            continue
        d = r.get("signal_date", "")
        tc = (r.get("transaction_costs") or {}).get("total_cost_usd")
        if d and tc:
            out[d] = float(tc)
    return out


# ── 账户 ─────────────────────────────────────────────────────────────────────

class Account:
    """极简账户对象：dict-backed，load/save/mark/assert。"""

    def __init__(self, strategy: str, data: dict):
        self.strategy = strategy
        self.data = data

    # -- io --------------------------------------------------------------
    @classmethod
    def load(cls, strategy: str) -> "Account | None":
        fp = account_path(strategy)
        if not os.path.exists(fp):
            return None
        with open(fp) as f:
            return cls(strategy, json.load(f))

    @classmethod
    def open_from_snapshot(cls, strategy: str, as_of: str, holdings: dict) -> "Account":
        """期初建账：快照持仓为 opening balance，cash = 1M − Σ成本（见 plan §4.5）。"""
        positions = {t: {"shares": h["shares"], "avg_cost": round(h["cost_basis"], 6),
                         "entry_date": h.get("entry_date", as_of)}
                     for t, h in holdings.items() if h["shares"]}
        cost_total = sum(p["shares"] * p["avg_cost"] for p in positions.values())
        cash = round(INITIAL_CASH - cost_total, 2)
        if cash < 0:
            raise AssertionError(f"[{strategy}] opening cash < 0 ({cash}) — 快照成本超过 $1M")
        data = {"as_of": as_of, "base_currency": "USD", "initial_cash": INITIAL_CASH,
                "cash": cash, "positions": positions,
                "cumulative_realized": 0.0, "cumulative_dividends": 0.0,
                "cumulative_fees": 0.0, "equity": None, "liabilities": {}}
        return cls(strategy, data)

    def save(self):
        with open(account_path(self.strategy), "w") as f:
            json.dump(self.data, f, indent=2)

    def save_history(self, day: str):
        os.makedirs(history_dir(self.strategy), exist_ok=True)
        fp = os.path.join(history_dir(self.strategy),
                          f"account_{self.strategy}_{day.replace('-', '')}.json")
        with open(fp, "w") as f:
            json.dump(self.data, f, indent=2)

    # -- accounting ops ----------------------------------------------------
    def trade(self, day: str, ticker: str, delta: int, price: float) -> dict:
        """delta>0 买入 / delta<0 卖出。返回 ledger 行。"""
        p = self.data["positions"].get(ticker, {"shares": 0, "avg_cost": 0.0,
                                                "entry_date": day})
        row = {"date": day, "ticker": ticker,
               "side": "BUY" if delta > 0 else "SELL",
               "shares": abs(int(delta)), "price": round(float(price), 4)}
        gross = abs(delta) * price
        if delta > 0:
            new_shares = p["shares"] + delta
            p["avg_cost"] = round((p["shares"] * p["avg_cost"] + delta * price)
                                  / new_shares, 6)
            if p["shares"] == 0:
                p["entry_date"] = day
            p["shares"] = new_shares
            self.data["cash"] = round(self.data["cash"] - gross, 2)
            row["gross"] = round(-gross, 2)
        else:
            qty = abs(delta)
            if qty > p["shares"]:
                raise AssertionError(f"[{self.strategy}] {day} SELL {ticker} {qty} > 持有 {p['shares']}")
            realized = round((price - p["avg_cost"]) * qty, 2)
            p["shares"] -= qty
            self.data["cash"] = round(self.data["cash"] + gross, 2)
            self.data["cumulative_realized"] = round(
                self.data["cumulative_realized"] + realized, 2)
            row["gross"] = round(gross, 2)
            row["avg_cost_at_trade"] = p["avg_cost"]
            row["realized_pnl"] = realized
        if p["shares"] == 0:
            self.data["positions"].pop(ticker, None)
        else:
            self.data["positions"][ticker] = p
        row["dedup_key"] = f"{day}-{ticker}-{row['side']}"
        return row

    def dividend(self, day: str, ticker: str, per_share: float) -> dict | None:
        p = self.data["positions"].get(ticker)
        if not p or p["shares"] <= 0 or per_share <= 0:
            return None
        total = round(p["shares"] * per_share, 2)
        self.data["cash"] = round(self.data["cash"] + total, 2)
        self.data["cumulative_dividends"] = round(
            self.data["cumulative_dividends"] + total, 2)
        return {"date": day, "ticker": ticker, "side": "DIV",
                "shares": p["shares"], "price": round(per_share, 6),
                "gross": total, "dedup_key": f"{day}-{ticker}-DIV"}

    def fee(self, day: str, amount: float) -> dict | None:
        if not amount or amount <= 0:
            return None
        self.data["cash"] = round(self.data["cash"] - amount, 2)
        self.data["cumulative_fees"] = round(self.data["cumulative_fees"] + amount, 2)
        return {"date": day, "ticker": "", "side": "FEE",
                "gross": round(-amount, 2), "dedup_key": f"{day}-FEE"}

    def mark(self, day: str, prices_row: pd.Series) -> float:
        """收盘 mark：equity = cash + Σ市值；恒等式断言。"""
        pos_val = 0.0
        for t, p in self.data["positions"].items():
            px = prices_row.get(t)
            if px is None or pd.isna(px):
                raise AssertionError(f"[{self.strategy}] {day} 缺 {t} 价格 — 拒绝 mark（宁缺毋假）")
            pos_val += p["shares"] * float(px)
        liab = sum(self.data.get("liabilities", {}).values()) if self.data.get("liabilities") else 0.0
        equity = round(self.data["cash"] + pos_val - liab, 2)
        # 恒等式：equity − 初始 = 已实现 + 分红 − 费用 + 未实现
        unrealized = round(pos_val - sum(p["shares"] * p["avg_cost"]
                                         for p in self.data["positions"].values()), 2)
        lhs = round(equity - self.data["initial_cash"], 2)
        rhs = round(self.data["cumulative_realized"] + self.data["cumulative_dividends"]
                    - self.data["cumulative_fees"] + unrealized, 2)
        if abs(lhs - rhs) > 0.05:
            raise AssertionError(
                f"[{self.strategy}] {day} 恒等式破裂: equity−1M={lhs} ≠ "
                f"realized+div−fees+unrealized={rhs}")
        self.data["equity"] = equity
        self.data["unrealized"] = unrealized
        self.data["position_value"] = round(pos_val, 2)
        self.data["as_of"] = day
        return equity


# ── 台账 ─────────────────────────────────────────────────────────────────────

def load_ledger_rows(strategy: str) -> list:
    fp = ledger_path(strategy)
    rows = []
    if os.path.exists(fp):
        with open(fp) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def append_ledger(strategy: str, rows: list, seen_keys: set):
    if not rows:
        return
    with open(ledger_path(strategy), "a") as f:
        for r in rows:
            k = r.get("dedup_key")
            if k in seen_keys:
                continue
            seen_keys.add(k)
            f.write(json.dumps(r) + "\n")


# ── 单日处理（replay 与每日 hook 共用同一路径）────────────────────────────────

def process_day(acct: Account, day: str, prices: pd.DataFrame,
                snaps: dict, divs_by_ticker: dict, fees_by_date: dict,
                seen_keys: set, mark_prices: pd.DataFrame | None = None) -> list:
    """一个交易日的完整记账：分红 → 快照差额成交（Polygon 当日收盘）→ 费用 → mark。
    返回当日 ledger 行（已去重过滤前的原始行）。

    mark_prices：收盘 mark 专用价格帧（真实 Close，见 load_store_prices）。
    None → 沿用 prices（旧行为）。本参数**只**影响 mark，不动任何成交/成本/已实现。"""
    strategy = acct.strategy
    ts = pd.Timestamp(day)
    if ts not in prices.index:
        return []                                   # 非交易日
    prices_row = prices.loc[ts]
    mark_row = prices_row if mark_prices is None or ts not in mark_prices.index \
        else mark_prices.loc[ts]
    rows: list = []

    # 1) 分红（ex-date 当日、按当前口径每股金额入现金）
    for t, divs in divs_by_ticker.items():
        for dv in divs:
            if dv["ex_dividend_date"] == day:
                r = acct.dividend(day, t, dv["_amount_current_caliber"])
                if r:
                    rows.append(r)

    # 2) 快照差额 → 成交（成交价 = Polygon 当日收盘；权威口径，见 plan §4.5-2）
    target = snaps.get(day)
    if target is not None:
        cur = {t: p["shares"] for t, p in acct.data["positions"].items()}
        tickers = sorted(set(cur) | set(target))
        # 先卖后买（现金流序：卖出所得可覆盖买入）
        deltas = {t: int(target.get(t, {}).get("shares", 0)) - int(cur.get(t, 0))
                  for t in tickers}
        for t in [t for t in tickers if deltas[t] < 0] + [t for t in tickers if deltas[t] > 0]:
            d = deltas[t]
            if d == 0:
                continue
            px = prices_row.get(t)
            if px is None or pd.isna(px):
                raise AssertionError(f"[{strategy}] {day} 成交缺 {t} 价格")
            rows.append(acct.trade(day, t, d, float(px)))

    # 2b) 费用：只看"这天有没有费用"，不再附加 target/deltas 条件。
    #     原判据 `if fee and any(deltas.values())` 嵌在快照块里，与 _catch_up_fees
    #     的补收规则不一致（同一笔费用两条路判法不同）。fees_by_date 只收录
    #     total_cost_usd 非零的调仓日，所以放开条件不会凭空多扣。
    #     去重仍由 dedup_key `{day}-FEE` 兜底 —— 已在 seen_keys 里的不会重复入账。
    fee = fees_by_date.get(day)
    if fee and f"{day}-FEE" not in seen_keys:
        r = acct.fee(day, fee)
        if r:
            rows.append(r)

    # 3) mark + 恒等式 + 落盘
    acct.mark(day, mark_row)
    acct.save_history(day)
    append_ledger(strategy, rows, seen_keys)
    return rows


def _prepare_dividends(strategy: str, tickers: list, start: str, end: str,
                       splits_by_ticker: dict) -> dict:
    """分红拉取 + 每股金额换算到当前口径（拆股前的 ex-date 金额 ÷ 因子）。"""
    raw = load_dividends_by_ticker(tickers, start, end)
    for t, divs in raw.items():
        for dv in divs:
            f = caliber_factor(t, dv["ex_dividend_date"], splits_by_ticker)
            dv["_amount_current_caliber"] = float(dv.get("cash_amount", 0) or 0) / f
    return raw


def _catch_up_fees(acct: Account, fees_by_date: dict, seen_keys: set) -> list:
    """补收此前漏记的费用（自愈；2026-08-20 加）。

    根因（已查实，不是判据写错）：daily_update 由 daily signal 的**尾部**调用，
    而当天的 daily report 是在它**之后**几秒才落盘的 ——
        AISS 2026-08-03  快照 18:42:12 / 报告 18:42:14
        SSRS 2026-08-03  快照 17:43:28 / 报告 17:43:31
    而 load_fees_by_date 读的正是那份报告 ⇒ 调仓日的交易成本当天一律看不见。
    daily_update 之后只处理 as_of 之后的日子，于是这笔费用永远没人回补。
    实测漏收：aiss 2026-08-03 $236.80、ssrs 2026-08-03 $138.51。
    （7/01 那几笔没漏，只是因为账本是次日才补建的 —— 见 account_history mtime。）

    窗口 `live_start < day <= as_of`：live_start 当天是 open_from_snapshot 的
    期初建仓日（replay §4.5-1 明确不合成虚拟交易），其成本不入账，故用严格大于。
    去重键与 process_day 同源 ⇒ 幂等，重复调用不会重复扣。
    现金即时扣减，equity 由随后那天的 mark 重算 —— 不改写任何历史快照。
    """
    live_start = _cfg(acct.strategy)["live_start"]
    rows = []
    for day in sorted(fees_by_date):
        if not (live_start < day <= acct.data["as_of"]):
            continue
        if f"{day}-FEE" in seen_keys:
            continue
        r = acct.fee(day, fees_by_date[day])
        if r:
            rows.append(r)
            log.warning(f"[{acct.strategy}] 补收漏记费用 {day} "
                        f"${fees_by_date[day]:,.2f}（当日报告晚于账本落盘）")
    return rows


def daily_update(strategy: str, upto: str | None = None) -> int:
    """每日增量：从 account.as_of 的次日补到最新快照日（含）。幂等。
    供 daily signal 尾部调用；replay 后的日常路径与 replay 同一 process_day。"""
    cfg = _cfg(strategy)
    acct = Account.load(strategy)
    if acct is None:
        log.warning(f"[{strategy}] 无账户文件——先运行 replay 建账")
        return 0
    splits = load_splits_by_ticker()
    snaps = load_snapshots(strategy, splits)
    if not snaps:
        return 0
    last_snap = max(snaps)
    upto = min(upto or last_snap, last_snap)
    if upto <= acct.data["as_of"]:
        return 0
    tickers = sorted({t for h in snaps.values() for t in h} |
                     set(acct.data["positions"]))
    prices = load_store_prices(strategy, tickers)
    mark_px = load_store_prices(strategy, tickers, field="Close")
    divs = _prepare_dividends(strategy, tickers, acct.data["as_of"], upto, splits)
    fees = load_fees_by_date(strategy)
    seen = {r.get("dedup_key") for r in load_ledger_rows(strategy)}
    days = [str(d.date()) for d in prices.index
            if acct.data["as_of"] < str(d.date()) <= upto]
    # 补收漏记费用：只在确有新交易日要处理时做，这样现金变动会被紧接着的
    # mark 收进**新的一天**，历史快照一律不动（口径迁移选的是"只向前"）。
    if days:
        append_ledger(strategy, _catch_up_fees(acct, fees, seen), seen)
    n = 0
    for day in days:
        process_day(acct, day, prices, snaps, divs, fees, seen, mark_prices=mark_px)
        n += 1
    acct.save()
    log.info(f"[{strategy}] ledger 更新 {n} 天 → as_of={acct.data['as_of']} "
             f"equity=${acct.data['equity']:,.2f}")
    return n
