"""
SelectPairs.py — 从 someopark 数据库的 pairs_day_select 集合中
筛选 15 对 MRPT 配对 + 15 对 MTFS 配对。

逻辑:
  MRPT (均值回归): 找跨天稳定出现的协整配对，优先 coint ∩ pca 交集
  MTFS (动量分化): 找 pca 中出现但协整性不稳定的配对（有因子关联但价差在发散）

用法:
  python SelectPairs.py              # 分析最近30天，输出推荐
  python SelectPairs.py --days 60    # 分析最近60天
  python SelectPairs.py --save       # 分析并覆写 pair_universe_*.json
"""

import argparse
import json
import os
import shutil
import sys
import time
from collections import defaultdict, Counter
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from db.connection import get_main_db


# ---------------------------------------------------------------------------
# 1. 读取数据库
# ---------------------------------------------------------------------------

def fetch_pair_records(days: int = 30):
    """从 pairs_day_select 集合读取最近 N 天的记录。"""
    db = get_main_db()
    col = db["pairs_day_select"]
    cursor = col.find({}, {"day": 1, "coint_pairs": 1, "similar_pairs": 1, "pca_pairs": 1}) \
               .sort("day", -1) \
               .limit(days)
    records = list(cursor)
    print(f"读取到 {len(records)} 天的配对记录")
    return records


# ---------------------------------------------------------------------------
# 1b. Latest-day signal boost: 协整/因子强度检验
# ---------------------------------------------------------------------------

def fetch_prices_bulk(tickers: list, lookback_days: int = 252) -> dict:
    """从 stock_data 批量拉取 ticker 的日收盘价。
    返回 {ticker: pd.Series(index=datetime, values=close)}。
    """
    import pandas as pd
    db = get_main_db()
    col = db["stock_data"]
    since_ms = int((time.time() - lookback_days * 1.5 * 86400) * 1000)  # 1.5x for weekends

    prices = {}
    for ticker in tickers:
        docs = list(
            col.find(
                {"symbol": ticker, "t": {"$gte": since_ms}},
                {"c": 1, "t": 1, "_id": 0}
            ).sort("t", 1)
        )
        if len(docs) < 60:  # need at least 60 data points
            continue
        dates = [datetime.fromtimestamp(d["t"] / 1000) for d in docs]
        closes = [float(d["c"]) for d in docs]
        s = pd.Series(closes, index=pd.DatetimeIndex(dates), name=ticker)
        s = s[~s.index.duplicated(keep='last')]  # dedup
        prices[ticker] = s.tail(lookback_days)
    return prices


def _compute_halflife(residuals) -> float:
    """OLS 残差的 AR(1) 半衰期（天）。"""
    import numpy as np
    res = np.array(residuals)
    lag = res[:-1]
    delta = np.diff(res)
    if len(lag) < 10 or np.std(lag) < 1e-10:
        return 999.0
    beta = np.polyfit(lag, delta, 1)[0]
    if beta >= 0:
        return 999.0  # no mean reversion
    return -np.log(2) / beta


def compute_coint_strength(s1_prices, s2_prices):
    """双方法协整检验 + 半衰期。
    返回 (combined_pvalue, halflife, strength_score)。
    strength_score ∈ [0, 1]。
    """
    import numpy as np
    from statsmodels.tsa.stattools import coint
    from statsmodels.tsa.vector_ar.vecm import coint_johansen

    n = min(len(s1_prices), len(s2_prices))
    if n < 60:
        return 1.0, 999.0, 0.0

    x = np.array(s1_prices[-n:], dtype=float)
    y = np.array(s2_prices[-n:], dtype=float)

    # Engle-Granger (both directions, take better p-value)
    try:
        _, p_xy, _ = coint(x, y)
        _, p_yx, _ = coint(y, x)
        eg_pval = min(p_xy, p_yx)
    except Exception:
        eg_pval = 1.0

    # Johansen trace test
    try:
        data = np.column_stack([x, y])
        jres = coint_johansen(data, det_order=0, k_ar_diff=1)
        # trace stat vs 5% critical for r=0 (no cointegration)
        j_reject = bool(jres.lr1[0] > jres.cvt[0, 1])
        # Approximate p-value from trace stat ratio
        j_pval = 0.01 if j_reject and jres.lr1[0] > jres.cvt[0, 2] else \
                 0.05 if j_reject else 0.20
    except Exception:
        j_pval = 1.0

    combined_pval = min(eg_pval, j_pval)

    # Halflife from OLS residuals
    try:
        beta = np.polyfit(x, y, 1)[0]
        residuals = y - beta * x
        halflife = _compute_halflife(residuals)
    except Exception:
        halflife = 999.0

    # Strength score
    coint_s = max(0.0, (0.05 - combined_pval) / 0.05)
    hl_s = max(0.0, (30.0 - halflife) / 30.0) if halflife < 999 else 0.0
    strength = coint_s * 0.7 + hl_s * 0.3

    return combined_pval, halflife, strength


def compute_factor_score(s1_prices, s2_prices):
    """PCA pair 的因子关联强度（用于 MTFS boost）。
    返回 (correlation, non_coint_pval, factor_score)。
    factor_score ∈ [-1, 1]。正值 = 好的 MTFS 候选，负值 = 惩罚。
    """
    import numpy as np

    n = min(len(s1_prices), len(s2_prices))
    if n < 60:
        return 0.0, 1.0, 0.0

    x = np.array(s1_prices[-n:], dtype=float)
    y = np.array(s2_prices[-n:], dtype=float)

    # 60d trailing correlation (on returns, not prices)
    rx = np.diff(np.log(np.maximum(x, 0.01)))
    ry = np.diff(np.log(np.maximum(y, 0.01)))
    if len(rx) >= 60:
        corr = float(np.corrcoef(rx[-60:], ry[-60:])[0, 1])
    else:
        corr = float(np.corrcoef(rx, ry)[0, 1])

    # Cointegration p-value (MTFS wants high p = NOT cointegrated)
    _, _, coint_strength = compute_coint_strength(x, y)

    # Factor score: ideal corr for MTFS is 0.1~0.4
    # corr=0.25 → peak, tapering to 0 at corr<-0.10 or corr>0.60
    if -0.10 <= corr <= 0.60:
        factor_s = max(0.0, 1.0 - abs(corr - 0.25) / 0.35)
    else:
        factor_s = 0.0

    # Non-cointegration bonus: coint_strength high → bad for MTFS
    non_coint_s = max(0.0, 1.0 - coint_strength)

    # Combined
    score = factor_s * 0.5 + non_coint_s * 0.5

    # Penalty: if strongly cointegrated today, penalize
    if coint_strength > 0.8:
        score = -0.5

    return corr, coint_strength, score


def boost_latest_day_signals(latest_record, coint_freq, similar_freq, pca_freq,
                              boost_weight: float = 3.0):
    """对最新一天的候选 pair 做协整/因子强度检验，将 boost 加到频率 dict 上。

    两个价格窗口：
      - 1 年 (252d)：和 pairs_day_select 的检验窗口一致
      - 4 个月 (80d)：近期关系强度，权重更高

    权重分配：1 年 × 0.4 + 4 个月 × 0.6
    """
    import pandas as pd

    day = latest_record.get("day", "?")
    coint_pairs = [normalize_pair(p) for p in (latest_record.get("coint_pairs") or []) if normalize_pair(p)]
    similar_pairs = [normalize_pair(p) for p in (latest_record.get("similar_pairs") or []) if normalize_pair(p)]
    pca_pairs_raw = [normalize_pair(p) for p in (latest_record.get("pca_pairs") or []) if normalize_pair(p)]

    # Collect all tickers needed
    all_tickers = set()
    for pool in [coint_pairs, similar_pairs, pca_pairs_raw]:
        for pair in pool:
            all_tickers.add(pair[0])
            all_tickers.add(pair[1])

    if not all_tickers:
        print(f"  [boost] 最新一天 ({day}) 无候选 pair，跳过")
        return

    print(f"\n--- Latest-day signal boost ({day}) ---")
    print(f"  协整={len(coint_pairs)} 相似={len(similar_pairs)} PCA={len(pca_pairs_raw)} ticker总数={len(all_tickers)}")

    # Fetch 1-year prices for all tickers
    prices = fetch_prices_bulk(list(all_tickers), lookback_days=252)
    print(f"  价格获取: {len(prices)}/{len(all_tickers)} tickers OK")

    def _dual_window_score(s1_prices, s2_prices, compute_fn):
        """在 1 年和 4 个月窗口上分别计算，加权合并。"""
        # 1-year window
        result_1yr = compute_fn(s1_prices, s2_prices)
        # 4-month window (tail 80 points)
        s1_4mo = s1_prices[-80:] if len(s1_prices) >= 80 else s1_prices
        s2_4mo = s2_prices[-80:] if len(s2_prices) >= 80 else s2_prices
        result_4mo = compute_fn(s1_4mo, s2_4mo)
        return result_1yr, result_4mo

    # ── Boost coint_pairs (MRPT) ──
    boosted_coint = 0
    for pair in coint_pairs:
        s1, s2 = pair
        if s1 not in prices or s2 not in prices:
            continue
        common_idx = prices[s1].index.intersection(prices[s2].index)
        if len(common_idx) < 60:
            continue
        p1 = prices[s1].loc[common_idx].values
        p2 = prices[s2].loc[common_idx].values

        r_1yr, r_4mo = _dual_window_score(p1, p2,
            lambda a, b: compute_coint_strength(a, b))
        pval_1yr, hl_1yr, score_1yr = r_1yr
        pval_4mo, hl_4mo, score_4mo = r_4mo

        boost = (score_1yr * 0.4 + score_4mo * 0.6) * boost_weight
        if boost > 0.01:
            coint_freq[pair] += boost
            boosted_coint += 1

    # ── Boost similar_pairs ──
    boosted_similar = 0
    for pair in similar_pairs:
        s1, s2 = pair
        if s1 not in prices or s2 not in prices:
            continue
        common_idx = prices[s1].index.intersection(prices[s2].index)
        if len(common_idx) < 60:
            continue
        p1 = prices[s1].loc[common_idx].values
        p2 = prices[s2].loc[common_idx].values

        r_1yr, r_4mo = _dual_window_score(p1, p2,
            lambda a, b: compute_coint_strength(a, b))
        _, _, score_1yr = r_1yr
        _, _, score_4mo = r_4mo

        boost = (score_1yr * 0.4 + score_4mo * 0.6) * boost_weight
        if boost > 0.01:
            similar_freq[pair] += boost
            boosted_similar += 1

    # ── Boost pca_pairs (MTFS) ──
    boosted_pca = 0
    penalized_pca = 0
    for pair in pca_pairs_raw:
        s1, s2 = pair
        if s1 not in prices or s2 not in prices:
            continue
        common_idx = prices[s1].index.intersection(prices[s2].index)
        if len(common_idx) < 60:
            continue
        p1 = prices[s1].loc[common_idx].values
        p2 = prices[s2].loc[common_idx].values

        r_1yr, r_4mo = _dual_window_score(p1, p2,
            lambda a, b: compute_factor_score(a, b))
        _, _, score_1yr = r_1yr
        _, _, score_4mo = r_4mo

        boost = (score_1yr * 0.4 + score_4mo * 0.6) * boost_weight
        pca_freq[pair] += boost  # can be negative (penalty)
        if boost > 0.01:
            boosted_pca += 1
        elif boost < -0.01:
            penalized_pca += 1

    print(f"  Boost 结果: 协整+{boosted_coint} 相似+{boosted_similar} PCA+{boosted_pca} PCA惩罚-{penalized_pca}")


# ---------------------------------------------------------------------------
# 近期动量：从 stock_data 查收盘价，计算 return 决定 s1/s2 方向
# ---------------------------------------------------------------------------

def fetch_returns(tickers: list, lookback_days: int = 30) -> dict:
    """从 stock_data 集合获取各 ticker 近 lookback_days 天的累计收益率。
    返回 {ticker: return_float}，缺数据的 ticker 不在字典里。
    stock_data 字段: symbol, c(收盘价), t(毫秒时间戳)
    """
    db = get_main_db()
    col = db["stock_data"]
    since_ms = int((time.time() - lookback_days * 86400) * 1000)

    returns = {}
    for ticker in tickers:
        docs = list(
            col.find(
                {"symbol": ticker, "t": {"$gte": since_ms}},
                {"c": 1, "t": 1, "_id": 0}
            ).sort("t", 1)
        )
        if len(docs) < 2:
            continue
        c_start = float(docs[0]["c"])
        c_end   = float(docs[-1]["c"])
        if c_start > 0:
            returns[ticker] = (c_end - c_start) / c_start
    return returns


def orient_pair(a: str, b: str, returns: dict):
    """根据近期 return 决定 (s1, s2)：s1 是动量赢家（涨更多），s2 是输家。
    如果某一方没有 return 数据，则保持字母序并标记 uncertain=True。
    返回 (s1, s2, ret_s1, ret_s2, uncertain)
    """
    ra = returns.get(a)
    rb = returns.get(b)
    if ra is None or rb is None:
        return a, b, ra, rb, True
    if ra >= rb:
        return a, b, ra, rb, False
    else:
        return b, a, rb, ra, False


def normalize_pair(pair) -> tuple:
    """将各种格式的配对统一成 (A, B) 其中 A < B（字母序）。
    支持格式: ["AAPL","META"] / ("AAPL","META") / {"s1":"AAPL","s2":"META"}
    """
    if isinstance(pair, dict):
        a = pair.get("s1") or pair.get("stock1") or pair.get("0", "")
        b = pair.get("s2") or pair.get("stock2") or pair.get("1", "")
    elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
        a, b = str(pair[0]), str(pair[1])
    else:
        return None
    if not a or not b:
        return None
    return tuple(sorted([a, b]))


# ---------------------------------------------------------------------------
# 2. 统计每对配对的跨天出现频率
# ---------------------------------------------------------------------------

def build_frequency_stats(records, decay_rate: float = 0.05):
    """返回三个 dict: coint_freq, similar_freq, pca_freq
    每个 dict: { (A, B): weighted_score }
    以及 total_weight (sum of all day weights, replaces total_days for rate calc).

    Recency weighting: records[0] is the most recent day (weight=1.0).
    Each older day decays by `decay_rate`:
      weight(i) = 1 / (1 + i * decay_rate)
      i=0 → 1.00,  i=5 → 0.80,  i=10 → 0.67,  i=20 → 0.50,  i=29 → 0.41

    This ensures recently-appearing pairs score higher than pairs that were
    active weeks ago but have since disappeared (stale cointegration / PCA).
    """
    coint_freq = defaultdict(float)
    similar_freq = defaultdict(float)
    pca_freq = defaultdict(float)

    total_weight = 0.0
    for i, rec in enumerate(records):  # records sorted by day desc
        w = 1.0 / (1.0 + i * decay_rate)
        total_weight += w

        for pair in (rec.get("coint_pairs") or []):
            key = normalize_pair(pair)
            if key:
                coint_freq[key] += w

        for pair in (rec.get("similar_pairs") or []):
            key = normalize_pair(pair)
            if key:
                similar_freq[key] += w

        for pair in (rec.get("pca_pairs") or []):
            key = normalize_pair(pair)
            if key:
                pca_freq[key] += w

    print(f"总天数: {len(records)}  (加权总权重: {total_weight:.1f}, decay_rate={decay_rate})")
    print(f"协整配对去重: {len(coint_freq)} 对")
    print(f"相似配对去重: {len(similar_freq)} 对")
    print(f"PCA配对去重:  {len(pca_freq)} 对")
    return coint_freq, similar_freq, pca_freq, total_weight


# ---------------------------------------------------------------------------
# 3. MRPT 筛选: 稳定均值回归者
# ---------------------------------------------------------------------------

def select_mrpt(coint_freq, similar_freq, pca_freq, total_days, n=15):
    """
    MRPT 需要价差稳定回归均值的配对。

    评分公式:
      score = coint_rate * 1.0       (协整出现率，最重要)
            + pca_rate   * 0.5       (PCA验证，Hurst<0.5+半衰期<21天)
            + similar_bonus * 0.3    (特征向量也相似，基本面支撑)

    筛选条件:
      - coint_rate >= 20%  (滚动协整数据轮换快，20%已代表较强稳定性)
      - 行业分散化 (同一 ticker 最多出现在 3 对中)
    """
    candidates = []

    for pair, coint_count in coint_freq.items():
        coint_rate = coint_count / total_days
        if coint_rate < 0.20:
            continue

        pca_count = pca_freq.get(pair, 0)
        pca_rate = pca_count / total_days

        similar_bonus = 1.0 if pair in similar_freq else 0.0

        score = coint_rate * 1.0 + pca_rate * 0.5 + similar_bonus * 0.3

        candidates.append({
            "pair": pair,
            "score": round(score, 4),
            "coint_rate": round(coint_rate, 2),
            "pca_rate": round(pca_rate, 2),
            "in_similar": pair in similar_freq,
            "coint_days": coint_count,
            "pca_days": pca_count,
        })

    # 按 score 降序
    candidates.sort(key=lambda x: -x["score"])

    # 行业分散化: 限制每个 ticker 最多出现 3 次
    selected = []
    ticker_count = Counter()

    for c in candidates:
        a, b = c["pair"]
        if ticker_count[a] >= 3 or ticker_count[b] >= 3:
            continue
        selected.append(c)
        ticker_count[a] += 1
        ticker_count[b] += 1
        if len(selected) >= n:
            break

    return selected


# ---------------------------------------------------------------------------
# 4. MTFS 筛选: 动量分化者
# ---------------------------------------------------------------------------

def select_mtfs(coint_freq, similar_freq, pca_freq, total_days, n=15):
    """
    MTFS 需要两只股票动量方向相反的配对。
    核心矛盾: someopark 的筛选器都是找 "相似/协整" 的配对,
              而 MTFS 需要 "有因子关联但协整性弱" 的配对。

    策略: 从 PCA 配对中找 "边缘对"
      - 有 PCA 因子关联 (同聚类), 但协整性不稳定
      - 这意味着价差在发散↔收敛之间切换 = 动量交易机会

    评分公式:
      score = pca_rate * (1.0 - coint_rate)
      - pca_rate 高: 有因子关联，不是随机噪声
      - coint_rate 低: 价差不回归，有趋势性

    筛选条件:
      - pca_rate >= 20%   (至少有因子关联)
      - coint_rate <= 40% (不能太协整，否则适合 MRPT)
      - 不能和 MRPT 已选配对重叠
    """
    candidates = []

    for pair, pca_count in pca_freq.items():
        pca_rate = pca_count / total_days
        if pca_rate < 0.20:
            continue

        coint_count = coint_freq.get(pair, 0)
        coint_rate = coint_count / total_days
        if coint_rate > 0.40:
            continue

        # 核心分数: pca_rate^2 拉开高频出现对的差距
        score = (pca_rate ** 2) * (1.0 - coint_rate)

        # similar 加分：特征向量也相似 = 基本面支撑的动量对，打破pca同分平局
        similar_count = similar_freq.get(pair, 0)
        similar_rate = similar_count / total_days
        score += similar_rate * 0.5

        # 偶有协整轻微惩罚
        if coint_count > 0:
            score *= 0.9

        candidates.append({
            "pair": pair,
            "score": round(score, 4),
            "pca_rate": round(pca_rate, 2),
            "coint_rate": round(coint_rate, 2),
            "similar_rate": round(similar_rate, 2),
            "pca_days": pca_count,
            "coint_days": coint_count,
            "similar_days": similar_count,
        })

    candidates.sort(key=lambda x: -x["score"])

    # 行业分散化
    selected = []
    ticker_count = Counter()

    for c in candidates:
        a, b = c["pair"]
        if ticker_count[a] >= 3 or ticker_count[b] >= 3:
            continue
        selected.append(c)
        ticker_count[a] += 1
        ticker_count[b] += 1
        if len(selected) >= n:
            break

    return selected


# ---------------------------------------------------------------------------
# 4b. MTFS v2: 动量排序 + 跨行业配对 + 反向过滤
# ---------------------------------------------------------------------------

def _fetch_price_matrix(tickers: list, lookback_days: int = 180):
    """从 stock_data 批量拉取 ticker 的日收盘价和成交量。
    返回 {ticker: {'close': pd.Series, 'volume': pd.Series}}。
    """
    import pandas as pd
    db = get_main_db()
    col = db["stock_data"]
    since_ms = int((time.time() - lookback_days * 1.5 * 86400) * 1000)

    result = {}
    for ticker in tickers:
        docs = list(
            col.find(
                {"symbol": ticker, "t": {"$gte": since_ms}},
                {"c": 1, "v": 1, "t": 1, "_id": 0}
            ).sort("t", 1)
        )
        if len(docs) < 60:
            continue
        dates = [datetime.fromtimestamp(d["t"] / 1000) for d in docs]
        closes = [float(d["c"]) for d in docs]
        volumes = [float(d.get("v", 0)) for d in docs]
        idx = pd.DatetimeIndex(dates)
        cs = pd.Series(closes, index=idx, name=ticker)
        vs = pd.Series(volumes, index=idx, name=ticker)
        cs = cs[~cs.index.duplicated(keep='last')].tail(lookback_days)
        vs = vs[~vs.index.duplicated(keep='last')].tail(lookback_days)
        result[ticker] = {'close': cs, 'volume': vs}
    return result


def _compute_momentum_score(closes):
    """复合动量分: 40d return × 0.6 + 120d return × 0.4。
    返回 (score, ret_40d, ret_120d) 或 None。
    """
    import numpy as np
    n = len(closes)
    if n < 40:
        return None

    vals = closes.values if hasattr(closes, 'values') else np.array(closes, dtype=float)

    # Sanity: current price must be positive
    if vals[-1] <= 0:
        return None

    # 40d return
    if n >= 41 and vals[-41] > 1.0:  # >$1 to avoid penny stock noise
        ret_40d = (vals[-1] - vals[-41]) / vals[-41]
    else:
        ret_40d = 0.0

    # 120d return (if available, else use all data)
    lookback = min(120, n - 1)
    if lookback >= 60 and vals[-(lookback + 1)] > 1.0:
        ret_120d = (vals[-1] - vals[-(lookback + 1)]) / vals[-(lookback + 1)]
    else:
        ret_120d = ret_40d  # fallback

    # Cap extreme returns (>200% or <-90% likely data error)
    ret_40d = max(-0.9, min(2.0, ret_40d))
    ret_120d = max(-0.9, min(3.0, ret_120d))

    score = ret_40d * 0.6 + ret_120d * 0.4
    return score, ret_40d, ret_120d


def _quality_filter_long(closes):
    """做多候选的质量过滤:
    - 过去 15 个交易日至少 5 天上涨
    - 上涨日的平均涨幅 > 0.3%
    - MACD 柱状图为正（短期动量加速）
    - RSI 14 在 40~80 之间（有动量但不过度超买）
    返回 (pass, details_dict)。
    """
    import numpy as np
    vals = closes.values if hasattr(closes, 'values') else np.array(closes, dtype=float)
    if len(vals) < 30:
        return False, {}

    # --- 15d up days ---
    last15 = vals[-16:]  # need 16 points for 15 daily returns
    daily_rets = np.diff(last15) / last15[:-1]
    up_days = np.sum(daily_rets > 0)
    avg_up_mag = float(np.mean(daily_rets[daily_rets > 0])) if np.any(daily_rets > 0) else 0.0

    if up_days < 5:
        return False, {'up_days': int(up_days), 'reason': 'up_days<5'}
    if avg_up_mag < 0.003:
        return False, {'up_days': int(up_days), 'avg_up_mag': avg_up_mag, 'reason': 'weak_up_mag'}

    # --- MACD (12, 26, 9) ---
    ema12 = _ema(vals, 12)
    ema26 = _ema(vals, 26)
    macd_line = ema12 - ema26
    signal_line = _ema(macd_line[-30:], 9)  # signal on recent portion
    macd_hist = macd_line[-1] - signal_line[-1]

    if macd_hist < 0:
        return False, {'macd_hist': float(macd_hist), 'reason': 'macd_negative'}

    # --- RSI 14 ---
    rsi = _rsi(vals, 14)
    if rsi < 35 or rsi > 85:
        return False, {'rsi': float(rsi), 'reason': f'rsi_out_of_range({rsi:.1f})'}

    return True, {
        'up_days': int(up_days), 'avg_up_mag': round(avg_up_mag, 4),
        'macd_hist': round(float(macd_hist), 4), 'rsi': round(float(rsi), 1),
    }


def _quality_filter_short(closes):
    """做空候选的质量过滤（做多的镜像）:
    - 过去 15 个交易日至少 5 天下跌
    - 下跌日的平均跌幅 > 0.3%
    - MACD 柱状图为负（短期动量减速）
    - RSI 14 在 20~60 之间（有弱势但不过度超卖）
    返回 (pass, details_dict)。
    """
    import numpy as np
    vals = closes.values if hasattr(closes, 'values') else np.array(closes, dtype=float)
    if len(vals) < 30:
        return False, {}

    # --- 15d down days ---
    last15 = vals[-16:]
    daily_rets = np.diff(last15) / last15[:-1]
    down_days = np.sum(daily_rets < 0)
    avg_down_mag = float(np.mean(np.abs(daily_rets[daily_rets < 0]))) if np.any(daily_rets < 0) else 0.0

    if down_days < 5:
        return False, {'down_days': int(down_days), 'reason': 'down_days<5'}
    if avg_down_mag < 0.003:
        return False, {'down_days': int(down_days), 'avg_down_mag': avg_down_mag, 'reason': 'weak_down_mag'}

    # --- MACD (12, 26, 9) ---
    ema12 = _ema(vals, 12)
    ema26 = _ema(vals, 26)
    macd_line = ema12 - ema26
    signal_line = _ema(macd_line[-30:], 9)
    macd_hist = macd_line[-1] - signal_line[-1]

    if macd_hist > 0:
        return False, {'macd_hist': float(macd_hist), 'reason': 'macd_positive'}

    # --- RSI 14 ---
    rsi = _rsi(vals, 14)
    if rsi < 15 or rsi > 65:
        return False, {'rsi': float(rsi), 'reason': f'rsi_out_of_range({rsi:.1f})'}

    return True, {
        'down_days': int(down_days), 'avg_down_mag': round(avg_down_mag, 4),
        'macd_hist': round(float(macd_hist), 4), 'rsi': round(float(rsi), 1),
    }


def _ema(vals, period):
    """Exponential moving average."""
    import numpy as np
    arr = np.array(vals, dtype=float)
    out = np.empty_like(arr)
    out[0] = arr[0]
    alpha = 2.0 / (period + 1)
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(vals, period=14):
    """RSI (Relative Strength Index)."""
    import numpy as np
    arr = np.array(vals, dtype=float)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss < 1e-10:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# Minimum average daily volume (shares) — filters out illiquid micro-caps
_MIN_AVG_DAILY_VOLUME = 300_000


def select_mtfs_v2(records, coint_freq, pca_freq, total_weight,
                    mrpt_pairs: set = None, n: int = 15):
    """MTFS v2: 动量排序 + 质量过滤 + 跨行业配对 + 协整/PCA 反向过滤。

    1. 从 30 天的 coint/similar/pca 池提取所有 ticker
    2. 拉取 120d+ 价格和成交量
    3. 复合动量分 (40d×0.6 + 120d×0.4)
    4. 做多候选: TOP 20% + 质量过滤 (up_days, MACD+, RSI 40-80)
    5. 做空候选: BOTTOM 20% + 质量过滤 (down_days, MACD-, RSI 20-60)
    6. 跨行业配对 + 排除协整/PCA 强关联对
    """

    # ── Step 1: ticker universe ──
    all_tickers = set()
    for rec in records:
        for pool in ['coint_pairs', 'similar_pairs', 'pca_pairs']:
            for p in (rec.get(pool) or []):
                key = normalize_pair(p)
                if key:
                    all_tickers.add(key[0])
                    all_tickers.add(key[1])

    print(f"\n--- MTFS v2 动量选对 ---")
    print(f"  Ticker 宇宙: {len(all_tickers)} 个")

    # ── Step 2: fetch prices + volume ──
    price_data = _fetch_price_matrix(list(all_tickers), lookback_days=180)
    print(f"  价格获取: {len(price_data)}/{len(all_tickers)} tickers OK")

    # ── Step 3: volume filter + momentum scoring ──
    scored = []
    vol_filtered = 0
    for ticker, data in price_data.items():
        avg_vol = float(data['volume'].mean()) if len(data['volume']) > 0 else 0
        if avg_vol < _MIN_AVG_DAILY_VOLUME:
            vol_filtered += 1
            continue

        result = _compute_momentum_score(data['close'])
        if result is None:
            continue
        score, ret_40d, ret_120d = result
        scored.append({
            'ticker': ticker,
            'score': score,
            'ret_40d': ret_40d,
            'ret_120d': ret_120d,
            'avg_vol': avg_vol,
            'sector': guess_sector(ticker),
        })

    scored.sort(key=lambda x: -x['score'])
    print(f"  动量评分: {len(scored)} tickers (volume 过滤掉 {vol_filtered})")

    # ── Step 4: TOP / BOTTOM pools + quality filters ──
    cutoff_top = max(5, len(scored) // 5)      # top 20%
    cutoff_bot = max(5, len(scored) // 5)      # bottom 20%

    top_candidates = scored[:cutoff_top]
    bottom_candidates = scored[-cutoff_bot:]

    top_pool = []
    top_rejected = 0
    for c in top_candidates:
        passed, details = _quality_filter_long(price_data[c['ticker']]['close'])
        if passed:
            c['quality'] = details
            top_pool.append(c)
        else:
            top_rejected += 1

    bottom_pool = []
    bottom_rejected = 0
    for c in bottom_candidates:
        passed, details = _quality_filter_short(price_data[c['ticker']]['close'])
        if passed:
            c['quality'] = details
            bottom_pool.append(c)
        else:
            bottom_rejected += 1

    # Bottom pool: sort by score ascending (weakest first)
    bottom_pool.sort(key=lambda x: x['score'])

    print(f"  TOP pool: {len(top_pool)}/{cutoff_top} (quality 过滤掉 {top_rejected})")
    print(f"  BOTTOM pool: {len(bottom_pool)}/{cutoff_bot} (quality 过滤掉 {bottom_rejected})")

    if len(top_pool) < 3 or len(bottom_pool) < 3:
        print(f"  ⚠ 候选不足，降级到旧 select_mtfs 逻辑")
        return None  # caller falls back to old method

    # ── Step 5: cross-sector pairing + reverse filters ──
    if mrpt_pairs is None:
        mrpt_pairs = set()

    selected = []
    ticker_count = Counter()
    used_top = set()
    used_bottom = set()

    for top in top_pool:
        if top['ticker'] in used_top:
            continue
        for bot in bottom_pool:
            if bot['ticker'] in used_bottom:
                continue

            # Same sector → skip (momentum spread too small)
            if top['sector'] == bot['sector'] and top['sector'] != 'other':
                continue

            # Ticker diversity: max 2 appearances per ticker
            if ticker_count[top['ticker']] >= 2 or ticker_count[bot['ticker']] >= 2:
                continue

            # Reverse filter: exclude cointegrated pairs
            pair_key = normalize_pair([top['ticker'], bot['ticker']])
            if pair_key and coint_freq.get(pair_key, 0) / total_weight > 0.30:
                continue

            # Reverse filter: exclude strong PCA pairs
            if pair_key and pca_freq.get(pair_key, 0) / total_weight > 0.50:
                continue

            # Exclude MRPT overlap
            if pair_key and pair_key in mrpt_pairs:
                continue

            # ✓ Select this pair
            sector = guess_pair_sector(top['ticker'], bot['ticker'])
            spread = top['score'] - bot['score']

            selected.append({
                'pair': pair_key,
                's1': top['ticker'],      # winner (long)
                's2': bot['ticker'],      # loser (short)
                'sector': sector,
                'score': round(spread, 4),
                'top_momentum': round(top['score'], 4),
                'bot_momentum': round(bot['score'], 4),
                'top_ret_40d': round(top['ret_40d'], 4),
                'bot_ret_40d': round(bot['ret_40d'], 4),
                'top_quality': top.get('quality', {}),
                'bot_quality': bot.get('quality', {}),
            })
            ticker_count[top['ticker']] += 1
            ticker_count[bot['ticker']] += 1
            used_top.add(top['ticker'])
            used_bottom.add(bot['ticker'])

            if len(selected) >= n:
                break
        if len(selected) >= n:
            break

    print(f"  配对结果: {len(selected)}/{n}")
    return selected


# ---------------------------------------------------------------------------
# 5. 去重: 确保 MRPT 和 MTFS 无重叠
# ---------------------------------------------------------------------------

def remove_overlap(mrpt_selected, mtfs_selected):
    """如果有重叠配对，从 MTFS 中移除（MRPT 优先）。"""
    mrpt_pairs = {c["pair"] for c in mrpt_selected}
    cleaned = [c for c in mtfs_selected if c["pair"] not in mrpt_pairs]
    removed = len(mtfs_selected) - len(cleaned)
    if removed:
        print(f"从 MTFS 中移除 {removed} 对与 MRPT 重叠的配对")
    return cleaned


# ---------------------------------------------------------------------------
# 6. 输出 & 保存
# ---------------------------------------------------------------------------

SECTOR_MAP = {
    # GICS-based sector mapping — covers S&P 500 + common traded tickers
    # Updated 2026-05 to eliminate "other" for all DB pool tickers
    "tech": {"AAPL", "META", "MSFT", "GOOGL", "GOOG", "NVDA", "AMD",
             "INTC", "CRM", "ORCL", "ADBE", "CSCO", "AVGO", "TXN", "QCOM",
             "MCHP", "ANET", "NOW", "SNPS", "CDNS", "KLAC", "LRCX", "MRVL",
             "MU", "NXPI", "ON", "PANW", "CRWD", "ZS", "FTNT", "NET", "DDOG",
             "SNOW", "PLTR", "PYPL", "INTU", "WDAY", "VEEV", "HUBS",
             "BILL", "MDB", "CFLT", "ESTC", "ZM", "DOCU", "OKTA",
             "FIVN", "APPN", "PATH",
             # S&P 500 IT additions
             "ADI", "AKAM", "APH", "CDW", "CTSH", "EPAM", "FFIV", "FICO",
             "FSLR", "GDDY", "GEN", "GLW", "HPQ", "KEYS", "MKTX", "MSI",
             "PAYC", "PTC", "TYL", "VRSN",
             },
    "finance": {"GS", "MS", "JPM", "BAC", "WFC", "C", "BLK", "BX", "KKR",
                "APO", "ARES", "CG", "OWL", "BN", "ALLY", "SCHW", "IBKR",
                "CME", "ICE", "TW", "NDAQ", "CBOE", "MCO", "SPGI", "MSCI",
                "FIS", "FISV", "GPN", "AXP", "V", "MA", "COF", "SYF", "DFS",
                "ACGL", "PGR", "ALL", "TRV", "AIG", "MET", "PRU", "AFL",
                "AMG", "BEN", "TROW", "IVZ", "EV", "UBS", "DB", "HSBC",
                "BCS", "RY", "TD", "BMO", "CM",
                # S&P 500 Financials additions
                "AIZ", "AJG", "AMP", "AON", "BK", "BR", "BRO", "CB", "CFG", "EG",
                "CINF", "FITB", "GL", "HBAN", "HIG", "JKHY", "KEY", "L",
                "MTB", "NTRS", "PFG", "PNC", "RF", "RJF", "STT", "TFC",
                "USB", "WRB", "WTW",
                },
    "energy": {"XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO",
               "PXD", "HES", "OXY", "DVN", "HAL", "BKR", "FANG", "APA",
               "MRO", "CTRA", "AR", "EQT", "USO", "XLE", "OIH", "AMLP",
               "WMB", "OKE", "KMI", "ET", "ENB", "TRP", "LNG",
               # S&P 500 Energy additions
               "TRGP",
               },
    "food": {# Consumer Staples (GICS) — food, beverage, household, retail staples
             "MCD", "YUM", "SBUX", "CMG", "DPZ", "QSR", "WEN", "JACK",
             "SHAK", "DG", "DLTR", "COST", "WMT", "TGT", "KR", "SYY",
             "US", "ADM", "BG", "MOS", "CF", "NTR", "FMC", "IFF",
             "KO", "PEP", "MNST", "KDP", "STZ", "SAM", "DEO", "BUD",
             "HSY", "MDLZ", "GIS", "K", "SJM", "CAG", "CPB", "HRL",
             "TSN", "PPC", "CALM", "SAFM",
             # S&P 500 Consumer Staples additions
             "CHD", "CL", "CLX", "KHC", "KMB", "KVUE", "MKC", "MO",
             "PG", "PM", "TAP",
             # Consumer Discretionary (GICS) — restaurants, retail, leisure
             "AMZN", "BKNG", "ABNB", "DASH", "CART", "UBER", "LYFT",
             "COIN", "SQ", "NFLX", "SPOT", "RBLX", "SHOP", "TEAM",
             "TWLO", "TTD", "PINS", "SNAP", "U", "SE", "MELI",
             "DKNG", "ROKU",
             # S&P 500 Consumer Discretionary additions
             "BBY", "EXPE", "F", "HAS", "HLT", "LOW", "LULU", "LVS",
             "MAR", "MGM", "MHK", "NCLH", "NVR", "PHM", "POOL",
             "RL", "TJX", "TSCO", "TSLA", "TTWO", "ULTA", "WYNN",
             "DRI", "MTCH",
             },
    "health": {"UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "TMO", "ABT",
               "DHR", "AMGN", "GILD", "BMY", "ISRG", "SYK", "BDX", "MDT",
               "BSX", "EW", "ZBH", "HCA", "UHS", "THC", "CNC", "MOH",
               "HUM", "CI", "ELV", "CVS", "WBA", "MCK", "CAH", "ABC",
               "A", "IQV", "CRL", "ALGN", "HOLX", "IDXX", "DXCM",
               "TDOC", "DOCS", "DGX",
               # S&P 500 Health Care additions
               "BAX", "BIIB", "COO", "COR", "DVA", "GEHC", "HSIC", "INCY",
               "LH", "MRNA", "MTD", "PODD", "REGN", "RMD", "STE", "TFX",
               "VLTO", "VRTX", "VTRS", "WAT",
               },
    "industrial": {"CAT", "DE", "HON", "MMM", "GE", "BA", "LMT", "RTX",
                   "NOC", "GD", "TDG", "HWM", "TXT", "LHX", "HII",
                   "UNP", "CSX", "NSC", "FDX", "UPS", "XPO", "JBHT",
                   "DAL", "UAL", "LUV", "AAL", "ALK", "SAVE", "HA",
                   "WM", "RSG", "WCN", "CLH", "EMR", "ROK", "AME",
                   "ETN", "PH", "ITW", "SWK", "IR", "CARR", "TT",
                   "JCI", "LII", "GNRC", "PWR", "FAST", "GWW", "WSO",
                   # S&P 500 Industrials additions
                   "AOS", "ALLE", "AXON", "BLDR", "BWA", "CHRW", "CMI", "CPRT",
                   "CTAS", "DOV", "EFX", "EXPD", "HUBB", "IEX", "LDOS",
                   "MAS", "NDSN", "ODFL", "PAYX", "PCAR", "PNR", "SNA",
                   "TDY", "TRMB", "WAB", "XYL",
                   },
    "realestate": {"AMT", "PLD", "CCI", "EQIX", "SPG", "O", "VICI",
                   "PSA", "EXR", "DLR", "AVB", "EQR", "MAA", "UDR",
                   "CPT", "ARE", "BXP", "SLG", "VNO", "KIM", "REG",
                   "FRT", "NNN", "WPC", "ADC", "STAG",
                   # S&P 500 Real Estate additions
                   "CBRE", "CSGP", "DOC", "ESS", "HST", "INVH", "IRM",
                   "VTR", "WELL",
                   },
    "materials": {"LIN", "APD", "SHW", "ECL", "DD", "DOW", "LYB",
                  "PPG", "NEM", "FCX", "SCCO", "AA", "NUE", "STLD",
                  "CLF", "X", "RS", "VMC", "MLM", "CX", "EXP", "SUM",
                  "IP", "PKG", "SEE", "BLL", "AVY", "SON", "GPK",
                  # S&P 500 Materials additions
                  "AMCR", "BALL", "CTVA", "EMN", "WY",
                  },
    "utilities": {"NEE", "DUK", "SO", "AEP", "SRE", "ED", "EXC",
                  "XEL", "WEC", "ES", "AWK", "ATO", "NI", "CMS",
                  "DTE", "FE", "PEG", "PPL", "ETR", "AES",
                  # S&P 500 Utilities additions
                  "AEE", "CNP", "D", "EIX", "EVRG", "LNT", "NRG",
                  "PCG", "PNW",
                  },
    "telecom": {"T", "VZ", "TMUS", "CHTR", "CMCSA", "LUMN", "FYBR",
                "ATUS", "SATS", "DISH",
                # S&P 500 Communication Services additions
                "DIS", "EA", "FOX", "FOXA", "LYV", "NWSA", "NWS",
                "OMC", "WBD",
                },
}


def guess_sector(ticker: str) -> str:
    """根据内置映射猜测 ticker 所属行业。"""
    for sector, tickers in SECTOR_MAP.items():
        if ticker in tickers:
            return sector
    return "other"


def guess_pair_sector(a: str, b: str) -> str:
    """取两只股票的行业（优先共同行业）。"""
    sa = guess_sector(a)
    sb = guess_sector(b)
    if sa == sb:
        return sa
    # 不同行业时，取非 other 的那个；都非 other 取第一个
    if sa == "other":
        return sb
    return sa


def format_mrpt_json(selected):
    """生成 pair_universe_mrpt.json 格式。"""
    result = []
    for c in selected:
        a, b = c["pair"]
        sector = guess_pair_sector(a, b)
        result.append({
            "s1": a,
            "s2": b,
            "sector": sector,
            "z_col": f"Z_{sector}",
        })
    return result


def format_mtfs_json(selected, returns: dict):
    """生成 pair_universe_mtfs.json 格式。
    s1 = 动量赢家（近期涨幅更大），s2 = 动量输家。
    """
    result = []
    for c in selected:
        a, b = c["pair"]
        sector = guess_pair_sector(a, b)
        s1, s2, _, _, uncertain = orient_pair(a, b, returns)
        entry = {
            "s1": s1,
            "s2": s2,
            "sector": sector,
            "spread_col": f"Momentum_Spread_{sector}",
        }
        if uncertain:
            entry["direction_uncertain"] = True
        result.append(entry)
    return result


def print_table(title, selected, strategy_type="mrpt", returns: dict = None):
    """打印候选配对表格。"""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

    if strategy_type == "mrpt":
        header = f"{'排名':>4}  {'配对':<16} {'行业':<12} {'得分':>6} {'协整率':>6} {'PCA率':>6} {'相似':>4} {'协整天':>5} {'PCA天':>5}"
        print(header)
        print("-" * 70)
        for i, c in enumerate(selected, 1):
            a, b = c["pair"]
            sector = guess_pair_sector(a, b)
            sim = "✓" if c.get("in_similar") else ""
            print(f"{i:>4}  {a+'/'+b:<16} {sector:<12} {c['score']:>6.3f} {c['coint_rate']:>5.0%} {c['pca_rate']:>5.0%} {sim:>4} {c['coint_days']:>5} {c['pca_days']:>5}")
    else:
        header = f"{'排名':>4}  {'s1→s2':<20} {'行业':<12} {'得分':>6} {'PCA率':>6} {'协整率':>6} {'相似率':>6} {'s1收益':>7} {'s2收益':>7}"
        print(header)
        print("-" * 90)
        for i, c in enumerate(selected, 1):
            a, b = c["pair"]
            sector = guess_pair_sector(a, b)
            sim_rate = c.get("similar_rate", 0.0)
            if returns is not None:
                s1, s2, rs1, rs2, uncertain = orient_pair(a, b, returns)
                direction = f"{s1}→{s2}" + ("?" if uncertain else "")
                rs1_str = f"{rs1:+.1%}" if rs1 is not None else "  N/A"
                rs2_str = f"{rs2:+.1%}" if rs2 is not None else "  N/A"
            else:
                direction = f"{a}/{b}"
                rs1_str = rs2_str = "  N/A"
            print(f"{i:>4}  {direction:<20} {sector:<12} {c['score']:>6.3f} {c['pca_rate']:>5.0%} {c['coint_rate']:>5.0%} {sim_rate:>5.0%} {rs1_str:>7} {rs2_str:>7}")


def save_json(data, filepath):
    # Backup existing file before overwriting
    if os.path.exists(filepath):
        base = os.path.dirname(filepath)
        history_dir = os.path.join(base, 'pair_universe_history')
        os.makedirs(history_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = os.path.basename(filepath)
        name, ext = os.path.splitext(filename)
        backup_path = os.path.join(history_dir, f"{name}_{ts}{ext}")
        shutil.copy2(filepath, backup_path)
        print(f"已备份: {backup_path}")
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"已保存: {filepath}")


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="从 someopark 数据库筛选 MRPT/MTFS 配对")
    parser.add_argument("--days", type=int, default=30, help="分析最近多少天 (默认30)")
    parser.add_argument("--n", type=int, default=15, help="每个策略选多少对 (默认15)")
    parser.add_argument("--save", action="store_true", help="覆写 pair_universe_*.json")
    args = parser.parse_args()

    # 读取数据
    records = fetch_pair_records(args.days)
    if not records:
        print("未读取到任何记录，请检查数据库连接")
        sys.exit(1)

    # 数据概览
    print("\n--- 数据概览 ---")
    for rec in records[:10]:
        day = rec.get("day", "?")
        nc = len(rec.get("coint_pairs") or [])
        ns = len(rec.get("similar_pairs") or [])
        np_ = len(rec.get("pca_pairs") or [])
        print(f"  {day}: 协整={nc} 相似={ns} PCA={np_}")
    if len(records) > 10:
        print(f"  ... 还有 {len(records) - 10} 天")

    # 构建频率统计
    coint_freq, similar_freq, pca_freq, total_days = build_frequency_stats(records)

    # Latest-day signal boost: 对最新一天的候选做协整/因子强度检验
    if records:
        boost_latest_day_signals(records[0], coint_freq, similar_freq, pca_freq,
                                  boost_weight=3.0)

    # MRPT 筛选
    mrpt_selected = select_mrpt(coint_freq, similar_freq, pca_freq, total_days, n=args.n)
    print_table(f"MRPT 推荐配对 (均值回归) — Top {args.n}", mrpt_selected, "mrpt")

    if len(mrpt_selected) < args.n:
        print(f"\n⚠ MRPT 仅筛出 {len(mrpt_selected)} 对 (目标 {args.n})，可尝试 --days 60 扩大样本")

    # MTFS v2: 动量排序 + 质量过滤 + 跨行业配对
    mrpt_pair_set = {c["pair"] for c in mrpt_selected}
    mtfs_v2 = select_mtfs_v2(records, coint_freq, pca_freq, total_days,
                              mrpt_pairs=mrpt_pair_set, n=args.n)

    if mtfs_v2 is not None:
        mtfs_selected = mtfs_v2
        # Print v2 table
        print(f"\n{'='*90}")
        print(f"  MTFS v2 推荐配对 (动量排序 + 跨行业配对) — Top {args.n}")
        print(f"{'='*90}")
        header = f"{'排名':>4}  {'s1(做多)→s2(做空)':<24} {'行业':<12} {'spread':>7} {'s1_40d':>7} {'s2_40d':>7} {'s1_MACD':>8} {'s2_MACD':>8} {'s1_RSI':>7} {'s2_RSI':>7}"
        print(header)
        print("-" * 100)
        for i, c in enumerate(mtfs_selected, 1):
            tq = c.get('top_quality', {})
            bq = c.get('bot_quality', {})
            print(f"{i:>4}  {c['s1']+'→'+c['s2']:<24} {c['sector']:<12} "
                  f"{c['score']:>+7.3f} {c.get('top_ret_40d',0):>+6.1%} {c.get('bot_ret_40d',0):>+6.1%} "
                  f"{tq.get('macd_hist',0):>+8.3f} {bq.get('macd_hist',0):>+8.3f} "
                  f"{tq.get('rsi',0):>7.1f} {bq.get('rsi',0):>7.1f}")
    else:
        # Fallback to old method
        print("\n⚠ MTFS v2 候选不足，降级到旧方法")
        mtfs_selected = select_mtfs(coint_freq, similar_freq, pca_freq, total_days, n=args.n + 5)
        mtfs_selected = remove_overlap(mrpt_selected, mtfs_selected)
        mtfs_selected = mtfs_selected[:args.n]
        all_mtfs_tickers = list({t for c in mtfs_selected for t in c["pair"]})
        returns = fetch_returns(all_mtfs_tickers, lookback_days=30)
        print_table(f"MTFS 推荐配对 (旧方法 fallback) — Top {args.n}", mtfs_selected, "mtfs", returns=returns)

    if len(mtfs_selected) < args.n:
        print(f"\n⚠ MTFS 仅筛出 {len(mtfs_selected)} 对 (目标 {args.n})")

    # 重叠检查
    mtfs_pair_set = {c.get("pair") or normalize_pair([c["s1"], c["s2"]]) for c in mtfs_selected}
    overlap = mrpt_pair_set & mtfs_pair_set
    print(f"\n两策略重叠配对: {len(overlap)} 对")

    # 保存
    if args.save:
        base = os.path.dirname(os.path.abspath(__file__))
        mrpt_json = format_mrpt_json(mrpt_selected)

        # MTFS v2 already has s1/s2 oriented (s1=winner, s2=loser)
        if mtfs_v2 is not None:
            mtfs_json = []
            for c in mtfs_selected:
                sector = c.get('sector', guess_pair_sector(c['s1'], c['s2']))
                mtfs_json.append({
                    "s1": c['s1'],
                    "s2": c['s2'],
                    "sector": sector,
                    "spread_col": f"Momentum_Spread_{sector}",
                })
        else:
            all_mtfs_tickers_fb = list({t for c in mtfs_selected for t in c["pair"]})
            returns_fb = fetch_returns(all_mtfs_tickers_fb, lookback_days=30)
            mtfs_json = format_mtfs_json(mtfs_selected, returns_fb)

        save_json(mrpt_json, os.path.join(base, "pair_universe_mrpt.json"))
        save_json(mtfs_json, os.path.join(base, "pair_universe_mtfs.json"))
        print("\n✓ pair_universe_mrpt.json 和 pair_universe_mtfs.json 已更新")
    else:
        print("\n提示: 加 --save 参数可覆写 pair_universe_*.json")


if __name__ == "__main__":
    main()
