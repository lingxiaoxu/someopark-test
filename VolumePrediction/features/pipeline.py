"""
features/pipeline — 58 步 A6-A16 的逐函数移植(Plan §四/附录 A;函数名与旧 notebook 一一对应)
================================================================================

面板契约(DEV_CONTRACTS):
    pd.DataFrame, MultiIndex(date: Timestamp, ticker: str) 已排序
    基础列: V(美元量=shares×vw), v(=log V), ma5_v, eta(=v−ma5_v, 目标)
    特征前缀: tech_ / fund1_ / fund2_ / cal_ / earn_

时间对齐语义(论文 §2.2 + 弱点⑨):
    目标 η_t 在 t 收盘后实现;特征 X_t 只能用 ≤ t−1 的信息。
    本管道的实现路径(与旧 notebook 等价,见 shift_volume_columns_and_drop_last):
      1) 各特征先按"窗口止于 t"计算(add_return_rollups 等);
      2) A14 shift_volume_columns_and_drop_last 把全部特征列按 ticker 下移 1 行
         并丢弃 shift 产生的首行 → 行 t 的特征全部来自 ≤ t−1。
    旧 notebook 的原始形式是"目标上移一行并丢最后一行"(y_t := η_{t+1});两种布局的
    (X, y) 配对逐元素相同——此处选择移特征、名字保留旧名以守保全契约,等价性由
    evaluation/lookahead_audit.prove_shift_correctness() 给出可证明断言。

目标基线定义(论文图 1 注,精确到位):
    [ma5]_t := (v_{t-1}+...+v_{t-5})/5 —— **不含当日**。
    因此 A14 之后 tech_v_ma5(行 t) == ma5_v(行 t),审计做此交叉断言。
"""
from __future__ import annotations

from datetime import date as _date
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

# ── 特征元数据: 每列 {group, min_lag}(min_lag=特征允许使用的最新信息距预测日的
#    最小滞后天数;A14 之后所有价量特征 min_lag≥1) ─────────────────────────────
EARN_BUCKETS = ["le_neg4", "neg3", "neg2", "neg1", "zero",
                "pos1", "pos2", "pos3", "pos4", "ge_pos5"]
TECH_WINDOWS = (1, 5, 22, 252)


def _validate_panel(panel: pd.DataFrame) -> None:
    if not isinstance(panel.index, pd.MultiIndex) or list(panel.index.names) != ["date", "ticker"]:
        raise ValueError("panel must have MultiIndex(date, ticker)")
    if not panel.index.is_monotonic_increasing:
        raise ValueError("panel index must be sorted (call panel.sort_index())")


def _by_ticker(s: pd.Series, fn) -> pd.Series:
    """按 ticker 分组应用 fn(保持原索引;fn 必须保序保形)。"""
    return s.groupby(level="ticker", group_keys=False).apply(fn)


# ─────────────────────────────────────────────────────────────────────────────
# A7-A9  add_volume_features / add_return_rollups
# ─────────────────────────────────────────────────────────────────────────────

def add_volume_features(panel: pd.DataFrame) -> pd.DataFrame:
    """A 组: v=log V、ma5_v(前5日均,不含当日)、eta=v−ma5_v(=目标)。

    V≤0(停牌/零量日)→ v=NaN → eta=NaN;此类行在训练前由调用方随填充策略处理
    (目标为 NaN 的行不参与训练,export 时保留并打标)。
    """
    _validate_panel(panel)
    out = panel.copy()
    V = out["V"].astype(float)
    v = pd.Series(np.where(V > 0, np.log(V.where(V > 0)), np.nan), index=out.index)
    out["v"] = v
    out["ma5_v"] = _by_ticker(v, lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    out["eta"] = out["v"] - out["ma5_v"]
    return out


def add_return_rollups(panel: pd.DataFrame) -> pd.DataFrame:
    """G3 tech 组(8 列): 过去 {1,5,22,252}d 的收益与对数成交量移动平均。

    本函数窗口**止于当日 t**(与旧 notebook 相同);A14 统一下移 1 行后成为
    "止于 t−1"的合规特征。需要 'ret' 列;缺失时从 'close' 逐票 pct_change 计算
    (调用方必须保证 close 为一致复权序列——拆股日假收益是数据毒药,见 §7.10-2)。
    """
    _validate_panel(panel)
    out = panel.copy()
    if "ret" not in out.columns:
        if "close" not in out.columns:
            raise ValueError("add_return_rollups needs 'ret' or (split-adjusted) 'close'")
        out["ret"] = _by_ticker(out["close"].astype(float), lambda s: s.pct_change())
    if "v" not in out.columns:
        raise ValueError("call add_volume_features first (needs 'v')")
    for w in TECH_WINDOWS:
        out[f"tech_ret_ma{w}"] = _by_ticker(
            out["ret"], lambda s, w=w: s.rolling(w, min_periods=w).mean())
        out[f"tech_v_ma{w}"] = _by_ticker(
            out["v"], lambda s, w=w: s.rolling(w, min_periods=w).mean())
    return out


# ─────────────────────────────────────────────────────────────────────────────
# A6  create_earnings_dummies
# ─────────────────────────────────────────────────────────────────────────────

def create_earnings_dummies(
    panel: pd.DataFrame,
    earnings_dates: Dict[str, Sequence],
    future_dates: Optional[Dict[str, Sequence]] = None,
) -> pd.DataFrame:
    """C 组(10 列 one-hot, earn_ 前缀): 距下一次已知财报日的**交易日**距离分桶。

    论文语义(§2.4 earnings): dist=0 → 当日即已知发布日;dist>0 → 距下次发布的
    交易日数;无未来预定时 dist=−(距上次发布的交易日数)。分桶:
    ≤−4, −3, −2, −1, 0, 1, 2, 3, 4, ≥5 → EARN_BUCKETS 一一对应。

    输入: earnings_dates/future_dates = {ticker: [date|Timestamp|str, ...]}
    (历史=MRPTFetchEarnings 缓存派生;未来=FMP 前瞻日历——由 inhouse_loader 提供,
    本函数不拉数据)。财报日若非该票交易日,按"其后第一个交易日=反应日"处理。
    某票完全无财报信息 → 该票 10 列全 0(信息缺失的诚实表达,论文零填充协议一致)。
    """
    _validate_panel(panel)
    out = panel.copy()
    for b in EARN_BUCKETS:
        out[f"earn_{b}"] = 0.0

    merged: Dict[str, np.ndarray] = {}
    for src in (earnings_dates or {}), (future_dates or {}):
        for tk, ds in src.items():
            arr = pd.to_datetime(pd.Index(list(ds))).normalize().values
            merged[tk] = np.union1d(merged.get(tk, np.array([], dtype="M8[ns]")), arr)

    def bucket_of(dist: int) -> str:
        if dist <= -4:
            return "le_neg4"
        if dist >= 5:
            return "ge_pos5"
        return {-3: "neg3", -2: "neg2", -1: "neg1", 0: "zero",
                1: "pos1", 2: "pos2", 3: "pos3", 4: "pos4"}[dist]

    tickers = out.index.get_level_values("ticker")
    dates_all = out.index.get_level_values("date")
    for tk, ann in merged.items():
        mask = tickers == tk
        if not mask.any() or len(ann) == 0:
            continue
        dts = dates_all[mask].values.astype("M8[ns]")
        n = len(dts)
        idx = np.arange(n)
        # 每个财报日 → 该票交易日序列中的"反应位"(非交易日财报 → 其后第一个交易日)。
        # 距离一律在**反应位空间**度量,修正周末/假日财报的当日归零语义。
        ann_pos = np.unique(np.searchsorted(dts, ann, side="left"))
        # 对位置 i: 下一个反应位(含当位) = 第一个 ann_pos ≥ i
        k = np.searchsorted(ann_pos, idx, side="left")
        has_next = k < len(ann_pos)
        kk = np.clip(k, 0, len(ann_pos) - 1)
        dist_next = ann_pos[kk] - idx
        # 无未来预定 → 距上一个反应位(负);k==0 且无 next 不可能(k=0→has_next)
        prev_pos = ann_pos[np.clip(k - 1, 0, len(ann_pos) - 1)]
        dist_prev = -(idx - prev_pos)
        dist = np.where(has_next, dist_next, dist_prev)
        buckets = np.array([bucket_of(int(d)) for d in dist])
        sub_index = out.index[mask]
        for b in EARN_BUCKETS:
            col = f"earn_{b}"
            sel = sub_index[buckets == b]
            if len(sel):
                out.loc[sel, col] = 1.0
    return out


# ─────────────────────────────────────────────────────────────────────────────
# A10  add_calendar_flags
# ─────────────────────────────────────────────────────────────────────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> pd.Timestamp:
    """某年某月第 n 个 weekday(0=周一..4=周五)。"""
    d = pd.Timestamp(year=year, month=month, day=1)
    off = (weekday - d.dayofweek) % 7
    return d + pd.Timedelta(days=off + 7 * (n - 1))


def add_calendar_flags(panel: pd.DataFrame) -> pd.DataFrame:
    """D 组(4 列, cal_ 前缀): 提前收市/三巫/双巫/罗素再平衡。

    规则(论文硬编码口径):
      triple_witching  = 3/6/9/12 月第 3 个周五
      double_witching  = 其余月份第 3 个周五
      russell_rebalance= 6 月第 4 个周五
      is_early_close   = NYSE 提前收市日(pandas_market_calendars 实取)
    规则日若非交易日(如 Juneteenth 撞上周五)→ 标记其前一交易日(文档化选择)。
    """
    _validate_panel(panel)
    import pandas_market_calendars as mcal
    out = panel.copy()
    dates = out.index.get_level_values("date")
    d0, d1 = dates.min(), dates.max()
    all_days = pd.DatetimeIndex(sorted(dates.unique()))

    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=d0.strftime("%Y-%m-%d"), end_date=d1.strftime("%Y-%m-%d"))
    closes_et = sched["market_close"].dt.tz_convert("America/New_York")
    early = set(pd.DatetimeIndex(
        sched.index[(closes_et.dt.hour * 60 + closes_et.dt.minute) < 16 * 60]).normalize())

    def to_trading(day: pd.Timestamp) -> Optional[pd.Timestamp]:
        pos = all_days.searchsorted(day, side="right") - 1
        if pos < 0:
            return None
        prev = all_days[pos]
        return day if day in all_days else prev

    triple, double, russell = set(), set(), set()
    for y in range(d0.year, d1.year + 1):
        for m in range(1, 13):
            f3 = _nth_weekday(y, m, 4, 3)
            td = to_trading(f3)
            if td is not None and d0 <= td <= d1:
                (triple if m in (3, 6, 9, 12) else double).add(td)
        f4 = _nth_weekday(y, 6, 4, 4)
        td = to_trading(f4)
        if td is not None and d0 <= td <= d1:
            russell.add(td)

    # 注意: np.isin 对 Timestamp 对象数组会静默失配 → 一律用 DatetimeIndex.isin
    norm = pd.DatetimeIndex(dates).normalize()
    out["cal_is_early_close"] = norm.isin(early).astype(float)
    out["cal_triple_witching"] = norm.isin(triple).astype(float)
    out["cal_double_witching"] = norm.isin(double).astype(float)
    out["cal_russell_rebalance"] = norm.isin(russell).astype(float)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# A11-A12  缺失填充(legacy 轨) + 双协议 fill()
# ─────────────────────────────────────────────────────────────────────────────

def fill_with_stock_past_median(panel: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    """逐票用**过去**中位数填 NaN(expanding median shift(1),零前视)。"""
    _validate_panel(panel)
    out = panel.copy()
    for c in cols:
        past_med = _by_ticker(out[c], lambda s: s.expanding(min_periods=1).median().shift(1))
        out[c] = out[c].fillna(past_med)
    return out


def fill_with_stock_global_median(panel: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    """逐票全样本中位数兜底(旧作行为;**含前视**,仅 legacy 轨使用并在
    G 差异报告留档);仍缺(全 NaN 票)→ 0。"""
    _validate_panel(panel)
    out = panel.copy()
    for c in cols:
        gmed = out[c].groupby(level="ticker").transform("median")
        out[c] = out[c].fillna(gmed).fillna(0.0)
    return out


def fill(panel: pd.DataFrame, cols: Iterable[str], policy: str = "paper") -> pd.DataFrame:
    """双缺失值协议(§7.2): paper=零填充(论文 A.1);legacy=过去中位→全局中位→0。"""
    cols = list(cols)
    if policy == "paper":
        out = panel.copy()
        out[cols] = out[cols].fillna(0.0)
        return out
    if policy == "legacy":
        out = fill_with_stock_past_median(panel, cols)
        return fill_with_stock_global_median(out, cols)
    raise ValueError(f"unknown fill policy: {policy!r}")


# ─────────────────────────────────────────────────────────────────────────────
# A13  zscore_normalize
# ─────────────────────────────────────────────────────────────────────────────

def zscore_normalize(
    panel: pd.DataFrame,
    cols: Iterable[str],
    train_end: Optional[str] = None,
    clip_z: float = 5.0,
) -> pd.DataFrame:
    """逐票时序 z 化,防前视。

    train_end 给定 → 用 ≤train_end 的训练窗均值/方差(论文单切分协议);
    train_end 为 None → 全因果 expanding 统计量(shift(1))。std≈0/NaN → z=0。
    clip_z: z 截断阈(默认 ±5,GKX 惯例)——训练窗 std 退化票(IPO 晚/低变异)
    否则产生 |z|>10³ 级离群,实测把 OLS 系数压平(2026-07-23 诊断)。
    """
    _validate_panel(panel)
    out = panel.copy()
    if train_end is not None:
        te = pd.Timestamp(train_end)
        for c in cols:
            s = out[c]
            tr = s[s.index.get_level_values("date") <= te]
            mu = tr.groupby(level="ticker").mean()
            sd = tr.groupby(level="ticker").std()
            tk = out.index.get_level_values("ticker")
            z = (s - mu.reindex(tk).values) / sd.reindex(tk).values
            z = z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
            out[c] = z.clip(-clip_z, clip_z) if clip_z else z
    else:
        for c in cols:
            def _causal_z(s: pd.Series) -> pd.Series:
                mu = s.expanding(min_periods=2).mean().shift(1)
                sd = s.expanding(min_periods=2).std().shift(1)
                return (s - mu) / sd
            z = _by_ticker(out[c], _causal_z)
            z = z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
            out[c] = z.clip(-clip_z, clip_z) if clip_z else z
    return out


# ─────────────────────────────────────────────────────────────────────────────
# A14  shift_volume_columns_and_drop_last
# ─────────────────────────────────────────────────────────────────────────────

def shift_volume_columns_and_drop_last(
    panel: pd.DataFrame,
    cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """前视对齐(弱点⑨核心;旧 notebook 同名步骤的等价实现)。

    把全部价量派生特征列(默认 tech_*)按 ticker **下移 1 行**,并丢弃 shift 产生的
    NaN 首行 → 行 t 的特征信息全部 ≤ t−1,目标仍为 η_t。
    与旧作"目标上移一行并 drop last"的 (X,y) 配对逐元素相同(名字保留守保全契约,
    等价性由 lookahead_audit.prove_shift_correctness() 断言)。
    cal_/earn_ 列**不**移位: 日历与财报日程在 t 日开盘前即为已知信息(前瞻日历),
    论文将其作为当日可观测特征。
    """
    _validate_panel(panel)
    out = panel.copy()
    if cols is None:
        cols = [c for c in out.columns if c.startswith("tech_")]
    for c in cols:
        out[c] = out[c].groupby(level="ticker").shift(1)
    # 丢弃因 shift 无特征的首行(每票第一行)
    first_mask = out.groupby(level="ticker").cumcount() == 0
    out = out[~first_mask]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 特征元数据(lookahead_audit 消费)
# ─────────────────────────────────────────────────────────────────────────────

def get_features_meta(panel: pd.DataFrame) -> Dict[str, dict]:
    meta: Dict[str, dict] = {}
    for c in panel.columns:
        if c.startswith("tech_"):
            meta[c] = {"group": "tech", "min_lag": 1}
        elif c.startswith("fund1_") or c.startswith("fund2_"):
            meta[c] = {"group": c.split("_")[0], "min_lag": 1, "pit": "acceptedDate"}
        elif c.startswith("cal_"):
            meta[c] = {"group": "calendar", "min_lag": 0}   # 日程性信息,当日已知
        elif c.startswith("earn_"):
            meta[c] = {"group": "earnings", "min_lag": 0}   # 前瞻日历,当日已知
    return meta
