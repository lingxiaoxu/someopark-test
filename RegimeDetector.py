"""
RegimeDetector.py — 市场状态检测器
=====================================
为 DailySignal.py 提供 regime 评分，决定 MRPT / MTFS 的动态权重。

MRPT = 均值回归 = 做空波动率 → 低波动、低信用利差、稳定利率 → 偏多
MTFS = 动量趋势 = 做多波动率 → 高波动、趋势明确、AI/动量热度高 → 偏多

Regime 信号来源：
  1. 市场波动率：VIX, MOVE (债券波动率)
  2. 信用利差：HY spread (FRED BAMLH0A0HYM2), IG spread (BAMLC0A0CM)
  3. 利率环境：10yr-2yr yield curve (T10Y2Y), Fed Funds (EFFR), 10yr breakeven
  4. 市场宽度：SPY vs IWM (大/小盘分化), QQQ/SPY ratio (tech premium)
  5. AI/动量含量：NVDA 20d return, ARKK 20d return, SOXX 20d return
  6. 宏观压力：St. Louis Financial Stress Index (STLFSI4), NFCI
  7. 地缘政治 proxy：GLD 20d return (避险资产), USO 20d return (油价)
  8. Dollar：UUP 20d return
  9. 策略自身 rolling vol (基于 OOS equity curve 历史)

输出：
  RegimeScore (0–100)  →  0=纯低波动(MRPT完全主导) 100=纯高波动(MTFS完全主导)
  mrpt_weight, mtfs_weight  →  portfolio allocation
  regime_label: 'risk_off_low_vol' / 'neutral' / 'risk_on_momentum' / 'stress'
  详细信号字典 (每个 indicator 的当前值和 z-score)

用法：
  from RegimeDetector import RegimeDetector
  rd = RegimeDetector(fred_api_key='...')
  result = rd.detect()
  print(result['regime_label'], result['mrpt_weight'], result['mtfs_weight'])
"""

import os
import logging
import warnings
from datetime import datetime, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger('RegimeDetector')

# ── 可选：MacroDataStore 百分位（本地20年历史）──────────────────────────────
try:
    from MacroDataStore import MacroDataStore as _MacroDataStore
    _MACRO_STORE = _MacroDataStore()
    _MACRO_STORE_AVAILABLE = True
except Exception:
    _MACRO_STORE_AVAILABLE = False

# ── 可选依赖 ──────────────────────────────────────────────────────────────────
try:
    from fredapi import Fred
    FREDAPI_AVAILABLE = True
except ImportError:
    FREDAPI_AVAILABLE = False
    log.warning("fredapi not installed — FRED macro indicators disabled. pip install fredapi")


# ── 指标定义 ──────────────────────────────────────────────────────────────────

# yfinance 指标：(ticker, field, description)
YF_INDICATORS = {
    # 波动率
    'vix':         ('^VIX',  'Close', 'Equity volatility (VIX)'),
    'move':        ('^MOVE', 'Close', 'Bond volatility (MOVE index)'),
    # 市场宽度 / 风险偏好
    'spy_20d':     ('SPY',   'Close', 'S&P 500 20d momentum'),
    'qqq_spy':     ('QQQ',   'Close', 'QQQ/SPY ratio (tech premium)'),
    'iwm_spy':     ('IWM',   'Close', 'IWM/SPY ratio (small/large divergence)'),
    'hyg':         ('HYG',   'Close', 'High-yield bond ETF'),
    # AI / 动量含量
    'nvda_20d':    ('NVDA',  'Close', 'NVIDIA 20d return (AI sentiment)'),
    'arkk_20d':    ('ARKK',  'Close', 'ARK Innovation 20d return (speculative)'),
    'soxx_20d':    ('SOXX',  'Close', 'Semiconductor 20d return'),
    'xlk_spy':     ('XLK',   'Close', 'XLK/SPY ratio (tech sector premium)'),
    # 地缘政治 / 避险
    'gld_20d':     ('GLD',   'Close', 'Gold 20d return (risk-off proxy)'),
    'uso_20d':     ('USO',   'Close', 'Oil 20d return (geopolitical)'),
    # 美元
    'uup_20d':     ('UUP',   'Close', 'Dollar 20d return'),
    # 10yr yield level
    'tnx':         ('^TNX',  'Close', '10-year Treasury yield'),
}

# FRED 指标：(series_id, description)
FRED_INDICATORS = {
    'hy_spread':      ('BAMLH0A0HYM2', 'High-yield credit spread (OAS)'),
    'ig_spread':      ('BAMLC0A0CM',   'IG credit spread (OAS)'),
    'yield_curve':    ('T10Y2Y',       '10yr - 2yr Treasury spread'),
    'effr':           ('EFFR',         'Effective Fed Funds Rate'),
    'breakeven_10y':  ('T10YIE',       '10yr inflation breakeven'),
    'breakeven_5y':   ('T5YIE',        '5yr inflation breakeven'),
    'fin_stress':     ('STLFSI4',      'St. Louis Financial Stress Index'),
    'nfci':           ('NFCI',         'National Financial Conditions Index'),
    'dgs10':          ('DGS10',        '10yr Treasury yield (daily)'),
    'dgs2':           ('DGS2',         '2yr Treasury yield (daily)'),
    'consumer_sent':  ('UMCSENT',      'U Michigan Consumer Sentiment (monthly)'),
    'recession_prob': ('USREC',        'NBER Recession Indicator'),
}

# ── 历史分位数边界（基于 2020-2026 长期数据，需要 rolling z-score）──────────
# 这些是 regime 判断的参考锚点，实际使用 z-score over 252d rolling window
REGIME_THRESHOLDS = {
    # VIX: 低=<15 中=15-25 高=25-35 极高=>35
    'vix':         {'low': 15, 'mid': 25, 'high': 35},
    # MOVE: 低=<80 中=80-100 高=100-120 极高=>120
    'move':        {'low': 80, 'mid': 100, 'high': 120},
    # HY spread: 低=<300bps 中=300-400 高=400-600 极高=>600
    'hy_spread':   {'low': 3.0, 'mid': 4.0, 'high': 6.0},
    # yield curve: inverted=<0 flat=0-0.5 normal=>0.5 steep=>1.5
    'yield_curve': {'inverted': 0.0, 'flat': 0.5, 'steep': 1.5},
    # Financial stress: 正值=压力 负值=宽松
    'fin_stress':  {'low': -0.5, 'mid': 0.0, 'high': 1.0},
}


class RegimeDetector:
    """
    检测当前市场 regime，输出 MRPT/MTFS 动态权重建议。

    Parameters
    ----------
    fred_api_key : str, optional
        FRED API key。若不提供则只用 yfinance 数据。
    lookback_days : int
        Rolling z-score 计算窗口（默认 252 个交易日）
    min_weight : float
        任一策略的最低权重（防止完全关闭，默认 0.2）
    mrpt_oos_curve : str, optional
        MRPT OOS equity curve CSV 路径（用于计算策略自身 rolling vol）
    mtfs_oos_curve : str, optional
        MTFS OOS equity curve CSV 路径
    """

    def __init__(
        self,
        fred_api_key: str | None = None,
        lookback_days: int = 252,
        min_weight: float = 0.15,
        mrpt_oos_curve: str | None = None,
        mtfs_oos_curve: str | None = None,
    ):
        self.fred_api_key  = fred_api_key or os.getenv('FRED_API_KEY', '')
        self.lookback_days = lookback_days
        self.min_weight    = min_weight
        self._fred         = None
        self._mrpt_curve   = mrpt_oos_curve
        self._mtfs_curve   = mtfs_oos_curve
        self._ind_history: dict = {}   # populated during _fetch_all_indicators

        # 预加载 VIX/MOVE 历史百分位（长期20年 + 近2年），用于自动计算分段边界
        # Fallback：若 MacroDataStore 不可用，使用从20年历史算出的固定值
        _VOL_PCT_FALLBACK = {
            'vix': {
                'long_term': {'p15': 12.67, 'p25': 13.74, 'median': 17.11, 'p75': 22.48, 'p85': 25.84},
                'recent_2y': {'p15': 14.31, 'p25': 15.12, 'median': 16.73, 'p75': 19.54, 'p85': 21.65},
            },
            'move': {
                'long_term': {'p15': 56.13, 'p25': 61.24, 'median': 77.29, 'p75': 103.60, 'p85': 118.40},
                'recent_2y': {'p15': 72.59, 'p25': 79.38, 'median': 92.65, 'p75': 101.45, 'p85': 108.14},
            },
        }
        self._vol_pct: dict = _VOL_PCT_FALLBACK.copy()
        self._vol_pct_short: dict = {}   # 近90天 hourly 百分位，空=不可用
        if _MACRO_STORE_AVAILABLE:
            try:
                self._vol_pct['vix']  = _MACRO_STORE.percentiles('vix')
                self._vol_pct['move'] = _MACRO_STORE.percentiles('move')
                log.debug(f"MacroDataStore percentiles loaded: vix={self._vol_pct['vix']}")
            except Exception as e:
                log.warning(f"MacroDataStore percentile load failed, using fallback values: {e}")
            try:
                self._vol_pct_short['vix']   = _MACRO_STORE.percentiles_short('vix')
                self._vol_pct_short['vxtlt'] = _MACRO_STORE.percentiles_short('vxtlt')
                log.debug(f"MacroDataStore short percentiles loaded: vix={self._vol_pct_short.get('vix')}")
            except Exception as e:
                log.warning(f"MacroDataStore short percentile load failed (will skip): {e}")

        if FREDAPI_AVAILABLE and self.fred_api_key:
            try:
                self._fred = Fred(api_key=self.fred_api_key)
                log.debug("FRED API connected")
            except Exception as e:
                log.warning(f"FRED API init failed: {e}")

        # 用历史日线数据回算 sub-score 序列，初始化 CISS 相关矩阵
        self._ind_history: dict = {}
        if _MACRO_STORE_AVAILABLE:
            self._bootstrap_ciss_history()

    def _bootstrap_ciss_history(self) -> None:
        """
        从 MacroDataStore 历史日线数据回算过去 lookback_days 天的 vol sub-score 序列，
        填充 _ind_history['vol_eq_*'] 和 _ind_history['vol_rt_*']，
        使 CISS 动态相关矩阵在第一次 detect() 时即可生效。

        只使用当时已知信息（rolling z-score 用截至当天的历史窗口），无前视偏差。
        """
        try:
            vix_s  = _MACRO_STORE.load('vix')
            move_s = _MACRO_STORE.load('move')
            if vix_s.empty or move_s.empty:
                return

            # 只取最近 lookback_days 个交易日（避免过长计算）
            n = min(self.lookback_days, len(vix_s))
            vix_s  = vix_s.iloc[-n:]
            move_s = move_s.iloc[-n:]

            # 只回算长期层（level + z-score），short_pct 是独立信号不进 CISS
            eq_hist: dict[str, list[float]] = {'vix_level': [], 'vix_z': []}
            rt_hist: dict[str, list[float]] = {'move_level': [], 'move_z': []}

            # 对齐两个序列的日期范围
            common_idx = vix_s.index.intersection(move_s.index)
            if len(common_idx) < 30:
                return

            vix_aligned  = vix_s.reindex(common_idx).values.astype(float)
            move_aligned = move_s.reindex(common_idx).values.astype(float)
            T = len(common_idx)

            w = self.lookback_days
            for i in range(T):
                win_start = max(0, i + 1 - w)
                vix_win  = vix_aligned[win_start: i + 1]
                move_win = move_aligned[win_start: i + 1]

                if len(vix_win) < 30:
                    continue

                v_lvl = float(vix_win[-1])
                v_mu  = float(vix_win.mean()); v_sd = float(vix_win.std())
                v_z   = float((v_lvl - v_mu) / v_sd) if v_sd > 0 else 0.0
                eq_hist['vix_level'].append(
                    self._vol_piecewise(v_lvl, self._vol_pct.get('vix', {}), 'vix'))
                eq_hist['vix_z'].append(
                    float(np.clip(0.50 - v_z * 0.10, 0.15, 0.70)))

                m_lvl = float(move_win[-1])
                m_mu  = float(move_win.mean()); m_sd = float(move_win.std())
                m_z   = float((m_lvl - m_mu) / m_sd) if m_sd > 0 else 0.0
                rt_hist['move_level'].append(
                    self._vol_piecewise(m_lvl, self._vol_pct.get('move', {}), 'move'))
                rt_hist['move_z'].append(
                    float(np.clip(0.50 - m_z * 0.10, 0.15, 0.70)))

            for k, lst in eq_hist.items():
                self._ind_history[f'vol_eq_{k}'] = lst
            for k, lst in rt_hist.items():
                self._ind_history[f'vol_rt_{k}'] = lst

            log.debug(f"CISS history bootstrapped: {T} days, "
                      f"eq={len(eq_hist['vix_level'])} pts, rt={len(rt_hist['move_level'])} pts")

        except Exception as e:
            log.warning(f"_bootstrap_ciss_history failed (CISS will use equal weights): {e}")

    # ── 数据获取 ───────────────────────────────────────────────────────────────

    def _fetch_yf(self, ticker: str, period: str = '400d') -> pd.Series:
        """Fetch yfinance Close series, returns daily price series."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
            if df.empty:
                return pd.Series(dtype=float)
            close = df['Close'].squeeze()
            return close.dropna()
        except Exception as e:
            log.warning(f"yfinance fetch {ticker} failed: {e}")
            return pd.Series(dtype=float)

    def _fetch_fred(self, series_id: str, days_back: int = 400,
                    retries: int = 3, retry_delay: float = 2.0) -> pd.Series:
        """Fetch FRED series with retry on transient errors (HTTP 500/503)."""
        import time
        if self._fred is None:
            return pd.Series(dtype=float)
        start = (datetime.today() - timedelta(days=days_back)).strftime('%Y-%m-%d')
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                s = self._fred.get_series(series_id, observation_start=start)
                return s.dropna()
            except Exception as e:
                last_exc = e
                err_str = str(e).lower()
                # Retry on server-side errors; give up on auth/not-found
                if any(x in err_str for x in ('internal server error', '500', '503',
                                               'service unavailable', 'timed out',
                                               'connection')):
                    if attempt < retries:
                        log.debug(f"FRED fetch {series_id} attempt {attempt} failed ({e}), "
                                  f"retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                        continue
                break
        log.warning(f"FRED fetch {series_id} failed: {last_exc}")
        return pd.Series(dtype=float)

    def _last_val(self, s: pd.Series) -> float | None:
        """Return last non-NaN value."""
        if s.empty:
            return None
        return float(s.iloc[-1])

    def _series_stats(self, s: pd.Series, freq: str = 'daily') -> dict:
        """
        Compute prev value, change, 30d/90d averages for a series.
        freq: 'daily' | 'weekly' | 'monthly'
        Returns dict with keys: prev_val, prev_date, change_abs, change_pct,
                                 avg30, avg90, vs30_pct, vs90_pct, freq,
                                 cur_date
        """
        if s.empty or len(s) < 2:
            return {'freq': freq}
        # Map freq → how many observations back = "previous"
        prev_step = {'daily': 1, 'weekly': 1, 'monthly': 1}[freq]
        cur_val  = float(s.iloc[-1])
        cur_date = str(s.index[-1].date()) if hasattr(s.index[-1], 'date') else str(s.index[-1])
        prev_idx = -(prev_step + 1)
        if len(s) < prev_step + 1:
            return {'freq': freq, 'cur_date': cur_date}
        prev_val  = float(s.iloc[-prev_step - 1])
        prev_date = str(s.index[-prev_step - 1].date()) if hasattr(s.index[-prev_step - 1], 'date') else str(s.index[-prev_step - 1])
        change_abs = cur_val - prev_val
        change_pct = (cur_val / prev_val - 1) if prev_val != 0 else None

        # 30-obs and 90-obs trailing averages (calendar-agnostic, uses observation count)
        # For daily series: 30obs≈30d, 90obs≈90d
        # For weekly: 30obs≈30w, but we want 30d/90d, so cap at series length
        # Use actual calendar days mapped to index:
        # Simpler: just use trailing n observations
        n30 = min(30, len(s))
        n90 = min(90, len(s))
        avg30 = float(s.iloc[-n30:].mean())
        avg90 = float(s.iloc[-n90:].mean())
        vs30_pct = (cur_val / avg30 - 1) if avg30 != 0 else None
        vs90_pct = (cur_val / avg90 - 1) if avg90 != 0 else None

        return {
            'freq':       freq,
            'cur_date':   cur_date,
            'prev_val':   round(prev_val, 6),
            'prev_date':  prev_date,
            'change_abs': round(change_abs, 6),
            'change_pct': round(change_pct, 6) if change_pct is not None else None,
            'avg30':      round(avg30, 6),
            'avg90':      round(avg90, 6),
            'vs30_pct':   round(vs30_pct, 6) if vs30_pct is not None else None,
            'vs90_pct':   round(vs90_pct, 6) if vs90_pct is not None else None,
        }

    def _rolling_zscore(self, s: pd.Series, window: int | None = None) -> float | None:
        """Z-score of last value relative to rolling window."""
        window = window or self.lookback_days
        if len(s) < 30:
            return None
        w = min(window, len(s))
        mu = float(s.iloc[-w:].mean())
        sd = float(s.iloc[-w:].std())
        if sd == 0:
            return 0.0
        return float((s.iloc[-1] - mu) / sd)

    def _pct_change_nd(self, s: pd.Series, n: int = 20) -> float | None:
        """n-day percentage return."""
        if len(s) < n + 1:
            return None
        return float(s.iloc[-1] / s.iloc[-n-1] - 1)

    def _strategy_vol_ratio(self) -> dict:
        """
        Compute 20-day rolling vol ratio between MTFS and MRPT strategies.
        Higher ratio → MTFS is relatively more volatile → prefer MRPT.
        """
        result = {'mrpt_vol': None, 'mtfs_vol': None, 'vol_ratio': None}
        try:
            if self._mrpt_curve and os.path.exists(self._mrpt_curve):
                mc = pd.read_csv(self._mrpt_curve, parse_dates=['Date'])
                mr = mc.set_index('Date')['DailyPnL']
                if len(mr) >= 20:
                    result['mrpt_vol'] = float(mr.iloc[-20:].std() * np.sqrt(252))

            if self._mtfs_curve and os.path.exists(self._mtfs_curve):
                mc = pd.read_csv(self._mtfs_curve, parse_dates=['Date'])
                mr = mc.set_index('Date')['OOS_DailyPnL']
                if len(mr) >= 20:
                    result['mtfs_vol'] = float(mr.iloc[-20:].std() * np.sqrt(252))

            if result['mrpt_vol'] and result['mtfs_vol'] and result['mrpt_vol'] > 0:
                result['vol_ratio'] = result['mtfs_vol'] / result['mrpt_vol']
        except Exception as e:
            log.debug(f"Strategy vol ratio failed: {e}")
        return result

    # ── 核心评分 ───────────────────────────────────────────────────────────────

    def _vol_piecewise(self, level: float, pct: dict, label: str) -> float:
        """
        倒U型波动率—策略偏好曲线（Avellaneda & Lee 2010; Ang & Bekaert 2002）。

        经济逻辑：
          - VIX 极低（<P15）：价差太小，信号稀少，MRPT 机会不足 → 偏 MTFS
          - VIX 中低~中高（P15~P85）：配对交易甜蜜区，价差充分但协整未破裂 → 偏 MRPT
            · 峰值偏 MRPT 的点设在 P50~P65 附近（中高波动率区）
          - VIX 极高（>P85）：arbitrage limits（Shleifer & Vishny 1997），协整破裂风险大 → 偏 MTFS
          · 极端上限以 P85+2×IQR 为封顶

        边界（混合长期×0.7 + 近2年×0.3，防止极端年份漂移）：
          very_low  = 长期P15 混合
          low       = 长期P25 混合
          high      = 长期P75 混合
          very_high = 长期P85 混合
          extreme   = very_high + 2×长期IQR

        输出分段（0=MRPT-favoring, 1=MTFS-favoring）：
          level < very_low  : 0.72→0.65  (极低，轻度 MTFS)
          very_low~low      : 0.65→0.25  (低区快速向 MRPT 过渡)
          low~high          : 0.25→0.30  (甜蜜区，MRPT 主导，峰值偏 MRPT)
          high~very_high    : 0.30→0.60  (高区，MTFS 渐增)
          very_high~extreme : 0.60→0.85  (极高，偏 MTFS)
          >extreme          : 0.85 (cap)
        """
        lt = pct.get('long_term', {})
        r2 = pct.get('recent_2y', {})

        if not (lt and 'p15' in lt and 'p75' in lt):
            log.warning(f"_vol_piecewise {label}: missing percentile keys, returning neutral")
            return 0.50

        def mix(key: str) -> float:
            return lt[key] * 0.7 + r2.get(key, lt[key]) * 0.3

        very_low  = mix('p15')
        low       = mix('p25')
        high      = mix('p75')
        very_high = mix('p85')
        iqr       = lt['p75'] - lt.get('p25', lt['p15'])
        extreme   = very_high + 2.0 * iqr if iqr > 0 else very_high * 1.4

        if level < very_low:
            # 极低区：0.72（level=0）→ 0.65（level=very_low）
            t = level / max(very_low, 1e-6)
            return float(np.clip(0.72 - t * 0.07, 0.60, 0.75))

        elif level < low:
            # 低区向甜蜜区过渡：0.65 → 0.25
            t = (level - very_low) / max(low - very_low, 1e-6)
            return float(0.65 - t * 0.40)

        elif level <= high:
            # 甜蜜区（MRPT 主导）：0.25 → 0.30，中部最低
            t = (level - low) / max(high - low, 1e-6)
            # 倒U底部：先降后升，峰值（最偏 MRPT）在区间 40% 处
            return float(0.25 + 4 * 0.05 * t * (1 - t))   # parabola: 0.25 at ends, 0.30 mid

        elif level <= very_high:
            # 高区：0.30 → 0.60
            t = (level - high) / max(very_high - high, 1e-6)
            return float(0.30 + t * 0.30)

        else:
            # 极高区：0.60 → 0.85，以 extreme 为封顶
            t = min((level - very_high) / max(extreme - very_high, 1e-6), 1.0)
            return float(np.clip(0.60 + t * 0.25, 0.60, 0.85))

    @staticmethod
    def _ciss_weights(scores_dict: dict) -> dict:
        """
        CISS-style 相关矩阵加权（Hollo, Kremer & Lo Duca 2012, ECB WP-1426）。

        权重 = 相关矩阵逆矩阵行和归一化：
          w_i ∝ Σ_j C^{-1}_{ij}
        高度相关的指标（如 level 和 z-score）自动降权；
        跨资产低相关指标（VIX vs MOVE）自动升权。

        使用过去 lookback 窗口内的 sub-score 历史相关矩阵（动态）。
        若历史不足或矩阵奇异，退化为等权。
        """
        keys = list(scores_dict.keys())
        n = len(keys)
        if n <= 1:
            return {k: 1.0 / max(n, 1) for k in keys}

        vals = np.array([scores_dict[k] for k in keys], dtype=float)
        # 所有分数相同则退化等权
        if np.all(vals == vals[0]):
            return {k: 1.0 / n for k in keys}

        # 用单位相关矩阵近似（离线 bootstrap）：构造对角为1、off-diagonal用值差异估计
        # 实际动态实现：将 sub-score 历史放入 _vol_score_history 后计算
        # 此处用基于先验知识的静态结构（见调用处动态覆盖）
        return {k: 1.0 / n for k in keys}

    def _ciss_aggregate(self, scores_dict: dict, history_key: str) -> float:
        """
        动态 CISS 加权聚合：用 _ind_history 中存储的历史 sub-score 序列
        计算滚动相关矩阵，然后用逆矩阵行和作为权重。

        history_key : 在 _ind_history 里查找各 sub-score 历史的前缀键
        若历史不足 30 个点，退化为等权均值（Fallback）。
        """
        keys = [k for k, v in scores_dict.items() if v is not None]
        if not keys:
            return 0.50
        if len(keys) == 1:
            return float(scores_dict[keys[0]])

        vals = np.array([scores_dict[k] for k in keys], dtype=float)

        # 尝试从历史中构建相关矩阵
        hist_matrix = []
        min_hist = 999
        for k in keys:
            hkey = f'{history_key}_{k}'
            hist = self._ind_history.get(hkey, [])
            hist_matrix.append(hist)
            min_hist = min(min_hist, len(hist))

        if min_hist >= 30:
            try:
                arr = np.array([h[-min_hist:] for h in hist_matrix], dtype=float)  # shape (n, T)
                # 相关矩阵
                corr = np.corrcoef(arr)
                # 正则化：防止奇异（加 0.1 对角，较强正则化减少极端权重）
                corr_reg = corr + 0.10 * np.eye(len(keys))
                inv_corr = np.linalg.inv(corr_reg)
                raw_w = inv_corr.sum(axis=1)
                # 负权重 clip 到 0，但保证每个指标至少 5% 权重（防止信息完全丢失）
                raw_w = np.clip(raw_w, 0, None)
                min_floor = 0.05
                # 先做归一化，再加 floor，再归一化
                total = raw_w.sum()
                if total > 1e-8:
                    weights = raw_w / total
                    # floor：每个指标至少 min_floor
                    weights = np.maximum(weights, min_floor)
                    weights = weights / weights.sum()
                    score = float(np.dot(weights, vals))
                    log.debug(f"CISS [{history_key}] weights={dict(zip(keys, weights.round(3)))} → {score:.3f}")
                    return float(np.clip(score, 0.0, 1.0))
            except np.linalg.LinAlgError:
                pass

        # Fallback：等权
        return float(np.mean(vals))

    def _short_pct_score(self, cp: float) -> float:
        """
        近90天 hourly 百分位 → sub-score（反转映射，均值回归逻辑）。

        经济逻辑（Avellaneda & Lee 2010）：
          近90天高位（cp>70）→ 短期峰值，均值回归概率高 → MRPT 有利 → score 低
          近90天低位（cp<30）→ 短期低点，可能继续下行或趋势 → MTFS 有利 → score 高

        分段（反转）：
          cp < 30   : score 0.65→0.50  (低位，轻度 MTFS)
          30–70     : score 0.50→0.25  (线性过渡到 MRPT)
          > 70      : score 0.25→0.15  (近期高位，强 MRPT)
        """
        if cp < 30:
            t = cp / 30.0
            return float(0.65 - t * 0.15)    # 0.65 → 0.50
        elif cp <= 70:
            t = (cp - 30) / 40.0
            return float(0.50 - t * 0.25)    # 0.50 → 0.25
        else:
            t = min((cp - 70) / 30.0, 1.0)
            return float(0.25 - t * 0.10)    # 0.25 → 0.15

    def _score_volatility(self, raw: dict) -> dict:
        """
        波动率 regime score（0=MRPT-favoring, 1=MTFS-favoring）

        架构（Guidolin & Timmermann 2008; Hollo et al. 2012）：
          equity_vol  ← VIX（level + z + short_pct），CISS 动态加权
          rates_vol   ← MOVE（level + z + vxtlt_short_pct），CISS 动态加权
          最终 = equity_vol × 0.65 + rates_vol × 0.35
          （配对交易主要暴露于股票风险，债券为次级；Avellaneda & Lee 2010）

        倒U型 level 曲线（见 _vol_piecewise）：
          极低 vol → MTFS（信号稀少）
          中等 vol → MRPT（甜蜜区）
          极高 vol → MTFS（协整破裂风险）

        z-score（1年 rolling）：高z → 处于历史高位将均值回归 → MRPT（反转信号）
        short_pct（90天 hourly）：高百分位 → 近期峰值 → MRPT（反转信号，已反转映射）

        参考：
          Avellaneda & Lee (2010) QF; Ang & Bekaert (2002) RFS;
          Hollo, Kremer & Lo Duca (2012) ECB WP-1426;
          Daniel & Moskowitz (2016) JFE; Shleifer & Vishny (1997) JF
        """
        # ── Equity Vol（VIX）──────────────────────────────────────────────────
        #
        # 分层设计（时间维度分离，防止 CISS 把跨时间尺度信号当竞争关系消除）：
        #   长期层（1年 z-score + 20年 level）→ CISS 加权 → eq_long
        #   短期层（90天 hourly 百分位）       → 独立信号  → eq_short
        #   equity_vol = eq_long × 0.60 + eq_short × 0.40
        #
        # 权重依据（Cochrane & Piazzesi 2005; Bekaert & Hoerova 2014）：
        #   长期信号捕获结构性 regime，短期信号捕获均值回归时机，两者独立互补

        eq_long: dict[str, float] = {}
        vix_lvl = raw.get('vix_level')
        if vix_lvl is not None:
            eq_long['vix_level'] = self._vol_piecewise(
                vix_lvl, self._vol_pct.get('vix', {}), 'vix'
            )
        vix_z = raw.get('vix_z')
        if vix_z is not None:
            eq_long['vix_z'] = float(np.clip(0.50 - vix_z * 0.10, 0.15, 0.70))

        # 存长期层历史供 CISS
        for k, v in eq_long.items():
            hkey = f'vol_eq_{k}'
            if hkey not in self._ind_history:
                self._ind_history[hkey] = []
            self._ind_history[hkey].append(v)
            if len(self._ind_history[hkey]) > self.lookback_days:
                self._ind_history[hkey] = self._ind_history[hkey][-self.lookback_days:]

        eq_long_score = self._ciss_aggregate(eq_long, 'vol_eq')

        # 短期层（独立，固定权重 0.40）
        eq_short_score: float | None = None
        vix_sp = self._vol_pct_short.get('vix', {})
        if vix_sp and 'current_pct' in vix_sp:
            eq_short_score = self._short_pct_score(vix_sp['current_pct'])

        if eq_short_score is not None:
            equity_vol = eq_long_score * 0.60 + eq_short_score * 0.40
        else:
            equity_vol = eq_long_score

        # ── Rates Vol（MOVE / VXTLT）────────────────────────────────────────────
        # 同样分层：长期（move_level + move_z）CISS 加权，短期（vxtlt_short_pct）独立

        rt_long: dict[str, float] = {}
        move_lvl = raw.get('move_level')
        if move_lvl is not None:
            rt_long['move_level'] = self._vol_piecewise(
                move_lvl, self._vol_pct.get('move', {}), 'move'
            )
        move_z = raw.get('move_z')
        if move_z is not None:
            rt_long['move_z'] = float(np.clip(0.50 - move_z * 0.10, 0.15, 0.70))

        for k, v in rt_long.items():
            hkey = f'vol_rt_{k}'
            if hkey not in self._ind_history:
                self._ind_history[hkey] = []
            self._ind_history[hkey].append(v)
            if len(self._ind_history[hkey]) > self.lookback_days:
                self._ind_history[hkey] = self._ind_history[hkey][-self.lookback_days:]

        rt_long_score = self._ciss_aggregate(rt_long, 'vol_rt')

        rt_short_score: float | None = None
        vxtlt_sp = self._vol_pct_short.get('vxtlt', {})
        if vxtlt_sp and 'current_pct' in vxtlt_sp:
            rt_short_score = self._short_pct_score(vxtlt_sp['current_pct'])

        if rt_short_score is not None:
            rates_vol = rt_long_score * 0.60 + rt_short_score * 0.40
        else:
            rates_vol = rt_long_score

        # ── 最终合成（equity 0.65 + rates 0.35）────────────────────────────────
        vol_composite = equity_vol * 0.65 + rates_vol * 0.35

        log.debug(f"vol_score: eq_long={eq_long_score:.3f} eq_short={eq_short_score} "
                  f"equity={equity_vol:.3f} | rt_long={rt_long_score:.3f} rt_short={rt_short_score} "
                  f"rates={rates_vol:.3f} | composite={vol_composite:.3f}")

        return {'vol_composite': float(np.clip(vol_composite, 0.0, 1.0))}

    def _score_credit(self, raw: dict) -> dict:
        """
        HY spread + IG spread → credit stress score
        高利差 → 信用紧张 → 不利于任何策略，但 MRPT 尤其怕（spread blow-up）
        中等利差升高 → 可能 MTFS 机会（trend）
        """
        scores = {}

        hy_z = raw.get('hy_spread_z')
        if hy_z is not None:
            # 利差扩大 (z>1.5) → 对 MRPT 更不利 → 增加 MTFS 权重
            scores['hy_spread'] = float(np.clip((hy_z + 0.5) / 2.5, 0, 1))

        ig_z = raw.get('ig_spread_z')
        if ig_z is not None:
            scores['ig_spread'] = float(np.clip((ig_z + 0.5) / 2.5, 0, 1))

        # HY absolute level
        hy_lvl = raw.get('hy_spread_level')
        if hy_lvl is not None:
            # <3%=0.1, 4%=0.5, 6%=0.9
            scores['hy_level'] = float(np.clip((hy_lvl - 2.5) / 4.0, 0.05, 1.0))

        return scores

    def _score_rates(self, raw: dict) -> dict:
        """
        Yield curve + Fed Funds + inflation breakeven → rate regime
        倒挂 → 经济压力 → MRPT 更差（配对在宏观压力下发散）
        宽松降息 → MRPT 更好（低波动，均值回归）
        """
        scores = {}

        # Yield curve: 倒挂很不好
        yc = raw.get('yield_curve_level')
        if yc is not None:
            # <0 (inverted)=0.7, 0.5=0.4, >1.5=0.2
            yc_score = float(np.clip(0.7 - (yc / 2.5), 0.05, 0.85))
            scores['yield_curve'] = yc_score

        # Fed Funds: 高利率 → MRPT 的 margin cost 更贵 → 稍微不利
        effr = raw.get('effr_level')
        if effr is not None:
            # <2%=0.3, 4%=0.5, 6%=0.7
            scores['effr'] = float(np.clip((effr - 1.5) / 5.0, 0.2, 0.75))

        # 10yr breakeven: 高通胀预期 → 不确定性上升 → MTFS 稍微更好
        bei = raw.get('breakeven_10y_level')
        if bei is not None:
            scores['breakeven'] = float(np.clip((bei - 1.5) / 2.5, 0.1, 0.8))

        return scores

    def _score_momentum_ai(self, raw: dict) -> dict:
        """
        AI/momentum/speculative 信号 → MTFS 偏向性
        NVDA/ARKK/SOXX 强劲 → 动量策略更有效
        """
        scores = {}

        nvda_20d = raw.get('nvda_20d')
        if nvda_20d is not None:
            # >+20% → strong AI momentum → MTFS (score 0.8+)
            # <-10% → AI unwind → MRPT
            scores['nvda'] = float(np.clip(0.5 + nvda_20d * 3.0, 0.05, 0.95))

        arkk_20d = raw.get('arkk_20d')
        if arkk_20d is not None:
            scores['arkk'] = float(np.clip(0.5 + arkk_20d * 2.5, 0.05, 0.95))

        soxx_20d = raw.get('soxx_20d')
        if soxx_20d is not None:
            scores['soxx'] = float(np.clip(0.5 + soxx_20d * 2.5, 0.05, 0.95))

        # QQQ/SPY ratio trend (tech premium)
        qqq_spy_z = raw.get('qqq_spy_z')
        if qqq_spy_z is not None:
            scores['tech_premium'] = float(np.clip((qqq_spy_z + 0.5) / 2.5, 0.1, 0.9))

        # XLK/SPY
        xlk_spy_z = raw.get('xlk_spy_z')
        if xlk_spy_z is not None:
            scores['xlk_premium'] = float(np.clip((xlk_spy_z + 0.5) / 2.5, 0.1, 0.9))

        return scores

    def _score_macro_stress(self, raw: dict) -> dict:
        """
        Financial stress / NFCI → 系统性风险
        极高压力 → 两策略都差，但 MRPT 更怕（spread blow-up）
        轻微压力 → MTFS 可能趋势机会
        """
        scores = {}

        fs = raw.get('fin_stress_level')
        if fs is not None:
            # <-0.5 (very easy)=0.25, 0=0.45, 1.0 (stress)=0.75, >2=0.9
            scores['fin_stress'] = float(np.clip(0.45 + fs * 0.25, 0.1, 0.9))

        nfci = raw.get('nfci_level')
        if nfci is not None:
            scores['nfci'] = float(np.clip(0.45 + nfci * 0.3, 0.1, 0.9))

        # Consumer sentiment: 低 → 市场不确定 → MTFS 更好
        cs = raw.get('consumer_sent_level')
        if cs is not None:
            # >90=0.25 (confident, low vol, MRPT good), 60=0.5, <50=0.75
            scores['consumer_sent'] = float(np.clip(0.5 + (70 - cs) / 80, 0.1, 0.85))

        return scores

    def _score_geopolitical(self, raw: dict) -> dict:
        """
        前向视角：开仓时预测未来持仓期（5-20天）价差是否会收敛（MRPT-favoring=低分）

        5个维度，全部用连续公式，无 hardcode 输出值：
        - 维度1: GLD-SPY corr20 + GLD_5d 修正（Baur & Lucey 2010: safe haven vs hedge）
        - 维度2: 油价来源 × VIX背景 × 加速度（Kilian 2009: supply/demand/speculative）
        - 维度3: USD × GLD 联动（Akram 2009: 真/假 risk-off 区分）
        - 维度4: 油价冲击持续性（Caldara 2022: GPR冲击大多 transient 1-3月）
        - 维度5: GLD level × momentum（risk-off 顶点检测，开仓最佳时机）
        """
        scores = {}

        gld_20d = raw.get('gld_20d')
        gld_5d  = raw.get('gld_5d')
        gld_z   = raw.get('gld_z')
        uso_20d = raw.get('uso_20d')
        uso_5d  = raw.get('uso_5d')
        uup_20d = raw.get('uup_20d')
        vix_z   = raw.get('vix_z')
        corr20  = raw.get('gld_spy_corr20')

        # ── 维度1：GLD-SPY corr20 + GLD_5d 修正（Baur & Lucey 2010）────────
        # corr20 < 0 → safe haven 触发，机构 flight to safety → 价差可能继续扩大
        # corr20 > 0 → hedge 模式，非系统性恐慌 → 价差倾向收敛
        # gld_5d 校正：safe haven 信号但黄金最近5日回落 → 正在消退 → 偏 MRPT
        if corr20 is not None:
            base = float(np.clip(0.5 - corr20 * 1.2, 0.10, 0.90))
            if gld_5d is not None:
                correction = float(np.clip(-gld_5d * 2.5, -0.15, 0.15))
                scores['gold_safe_haven'] = float(np.clip(base + correction, 0.10, 0.90))
            else:
                scores['gold_safe_haven'] = base

        # ── 维度2：油价来源分解（Kilian 2009）────────────────────────────────
        # USO 涨幅规模 × VIX背景（supply/geo vs demand）× 加速度（投机信号）
        if uso_20d is not None and vix_z is not None:
            # 涨幅规模 → 基础不确定性（涨得越多越不稳定）
            mag_score = float(np.clip(0.35 + uso_20d * 1.5, 0.20, 0.70))
            # VIX 背景：高VIX=供给/地缘冲击(transient,小幅加)，低VIX=需求驱动(减)
            vix_mod = float(np.clip(vix_z * 0.04, -0.10, 0.10))
            # 加速度修正：5日日均 vs 20日日均，加速=投机，最不稳定
            accel_mod = 0.0
            if uso_5d is not None and uso_20d != 0:
                daily_20  = uso_20d / 20
                daily_5   = uso_5d  / 5
                accel_ratio = (daily_5 / daily_20) if daily_20 != 0 else 1.0
                accel_mod = float(np.clip((accel_ratio - 1.0) * 0.08, -0.05, 0.15))
            scores['oil_source'] = float(np.clip(mag_score + vix_mod + accel_mod, 0.15, 0.75))

        # ── 维度3：美元+黄金联动（Akram 2009 三角传导）──────────────────────
        # UUP↑ + GLD↑ → 真 flight to safety → 价差受压
        # UUP↑ + GLD↓ → 美国经济强，非 risk-off → 价差稳定
        # 用各自 return 归一化后的乘积作为联动信号
        if uup_20d is not None and gld_20d is not None:
            uup_norm = uup_20d / 0.02   # 归一化（UUP月典型波动约±2%）
            gld_norm = gld_20d / 0.05   # 归一化（GLD月典型波动约±5%）
            joint    = uup_norm * gld_norm  # 正=同向, 负=反向
            if uup_20d > 0:
                # 美元涨：joint>0(黄金也涨)=真risk-off; joint<0(黄金跌)=经济强
                scores['usd_gold_joint'] = float(np.clip(0.50 + joint * 0.12, 0.15, 0.85))
            else:
                # 美元跌：风险偏好改善，整体偏 MRPT
                scores['usd_gold_joint'] = float(np.clip(0.35 + joint * 0.08, 0.15, 0.60))

        # ── 维度4：油价冲击持续性（Caldara 2022: GPR冲击大多 transient）──────
        # 5日日均速率 vs 20日日均速率：减速=冲击消化中=价差收敛; 加速=冲击未止
        if uso_20d is not None and uso_5d is not None:
            daily_20 = uso_20d / 4    # 20d折算成5d等价
            decel    = daily_20 - uso_5d   # 正=减速，负=加速
            shock    = abs(uso_20d)        # 冲击规模
            scores['oil_persistence'] = float(
                np.clip(0.45 - decel * 2.0 - shock * 0.15, 0.15, 0.70)
            )

        # ── 维度5：黄金 level × momentum（risk-off 顶点检测）────────────────
        # gld_z高（历史高位）+ gld_5d负（下跌）= risk-off peaked = MRPT 最佳入场时机
        # gld_z高 + gld_5d正（仍在涨）= risk-off 仍构建中 = 等待
        if gld_z is not None and gld_5d is not None:
            level_factor    = float(np.clip(gld_z  * 0.08, -0.15, 0.20))
            momentum_factor = float(np.clip(gld_5d * 3.00, -0.20, 0.20))
            scores['gold_level_momentum'] = float(
                np.clip(0.40 + level_factor + momentum_factor, 0.10, 0.75)
            )

        return scores

    def _score_strategy_vol(self, vol_info: dict) -> dict:
        """
        基于策略自身历史 rolling vol 的 vol-parity 权重
        """
        scores = {}
        ratio = vol_info.get('vol_ratio')
        if ratio is not None and ratio > 0:
            # ratio > 1 means MTFS is more volatile than MRPT → prefer MRPT
            # vol parity 逻辑: w_mtfs = 1 / (1 + ratio)
            scores['vol_parity'] = float(np.clip(ratio / (1 + ratio), 0.1, 0.9))
        return scores

    # ── 主入口 ─────────────────────────────────────────────────────────────────

    def detect(self, as_of: str | None = None) -> dict:
        """
        Run full regime detection. Returns dict with:
          regime_score    : 0-100 (0=MRPT dominant, 100=MTFS dominant)
          regime_label    : str
          mrpt_weight     : float (0-1)
          mtfs_weight     : float (0-1)
          indicators      : dict of all raw indicator values
          component_scores: dict of sub-scores per category
          weight_rationale: human-readable explanation
          as_of           : timestamp of detection
        """
        log.info("Running regime detection...")

        # 1. Fetch all raw data
        raw = self._fetch_all_indicators()

        # 2. Strategy vol ratio
        vol_info = self._strategy_vol_ratio()

        # 3. Score each category
        vol_scores   = self._score_volatility(raw)
        credit_scores = self._score_credit(raw)
        rate_scores  = self._score_rates(raw)
        ai_scores    = self._score_momentum_ai(raw)
        macro_scores = self._score_macro_stress(raw)
        geo_scores   = self._score_geopolitical(raw)
        strat_scores = self._score_strategy_vol(vol_info)

        # 4. Category weights (tuned based on MRPT/MTFS nature)
        #    All scores: 0=MRPT-favoring, 1=MTFS-favoring
        category_weights = {
            'volatility':    0.25,   # VIX/MOVE — 最直接的信号
            'credit':        0.18,   # 信用利差 — MRPT最怕spread blow-up
            'rates':         0.10,   # 利率环境
            'momentum_ai':   0.18,   # AI/动量含量 — MTFS的主要 alpha 来源
            'macro_stress':  0.12,   # 宏观金融压力
            'geopolitical':  0.09,   # 地缘/避险
            'strategy_vol':  0.08,   # 策略自身 vol parity
        }

        all_category_scores = {
            'volatility':    vol_scores,
            'credit':        credit_scores,
            'rates':         rate_scores,
            'momentum_ai':   ai_scores,
            'macro_stress':  macro_scores,
            'geopolitical':  geo_scores,
            'strategy_vol':  strat_scores,
        }

        # 5. Aggregate within each category (equal weight among sub-indicators)
        category_aggregated = {}
        for cat, sub_scores in all_category_scores.items():
            vals = [v for v in sub_scores.values() if v is not None]
            category_aggregated[cat] = float(np.mean(vals)) if vals else 0.5  # neutral if no data

        # 6. Final regime score (weighted sum, 0-1)
        regime_raw = 0.0
        total_weight = 0.0
        for cat, w in category_weights.items():
            if cat in category_aggregated:
                regime_raw += category_aggregated[cat] * w
                total_weight += w
        regime_score_01 = regime_raw / total_weight if total_weight > 0 else 0.5
        regime_score = round(regime_score_01 * 100, 1)

        # 7. Convert to weights with min_weight floor
        #    Linear mapping: score=0 → mrpt=1-min, mtfs=min
        #                    score=100 → mrpt=min, mtfs=1-min
        w_range  = 1.0 - 2 * self.min_weight
        mtfs_w   = self.min_weight + w_range * regime_score_01
        mrpt_w   = 1.0 - mtfs_w
        mrpt_w   = round(mrpt_w, 3)
        mtfs_w   = round(mtfs_w, 3)

        # 8. Regime label
        if regime_score < 25:
            label = 'risk_off_low_vol'       # VIX low, credit tight, MRPT dominant
        elif regime_score < 40:
            label = 'low_vol_mild_momentum'
        elif regime_score < 60:
            label = 'neutral'
        elif regime_score < 75:
            label = 'risk_on_momentum'       # AI hot, trend strong, MTFS dominant
        else:
            label = 'high_stress_or_momentum'  # very high VIX OR very strong momentum

        # 9. Stress override: extreme fear (VIX > 40) → reduce both, but MTFS more
        vix_lvl = raw.get('vix_level')
        stress_note = ''
        if vix_lvl is not None and vix_lvl > 40:
            label = 'extreme_stress'
            # In extreme stress, momentum works but is very volatile
            # keep current weights but note it
            stress_note = f' ⚠ VIX={vix_lvl:.1f} EXTREME — treat weights with caution'

        # 10. Rationale text
        rationale = self._build_rationale(raw, category_aggregated, regime_score, mrpt_w, mtfs_w)
        rationale += stress_note

        result = {
            'as_of':           as_of or datetime.today().strftime('%Y-%m-%d'),
            'regime_score':    regime_score,
            'regime_label':    label,
            'mrpt_weight':     mrpt_w,
            'mtfs_weight':     mtfs_w,
            'indicators':      raw,
            'component_scores': {
                cat: {
                    'aggregate': round(category_aggregated.get(cat, 0.5), 4),
                    'sub':       {k: round(v, 4) for k, v in sub.items() if v is not None},
                    'weight':    category_weights.get(cat, 0),
                }
                for cat, sub in all_category_scores.items()
            },
            'strategy_vol':    vol_info,
            'weight_rationale': rationale,
            'indicator_history': self._ind_history,
        }

        log.info(f"Regime: {label}  score={regime_score}  MRPT={mrpt_w:.0%}  MTFS={mtfs_w:.0%}")
        return result

    def _fetch_all_indicators(self) -> dict:
        """Fetch all raw indicator values. Returns flat dict."""
        raw = {}
        # 保留 bootstrap 的 CISS 历史（vol_eq_* / vol_rt_*），只清除 daily 指标快照
        ciss_keys = {k: v for k, v in self._ind_history.items()
                     if k.startswith('vol_eq_') or k.startswith('vol_rt_')}
        self._ind_history = ciss_keys

        # ── yfinance batch fetch ───────────────────────────────────────────
        tickers_needed = {
            'vix':  '^VIX',
            'move': '^MOVE',
            'spy':  'SPY',
            'qqq':  'QQQ',
            'iwm':  'IWM',
            'hyg':  'HYG',
            'nvda': 'NVDA',
            'arkk': 'ARKK',
            'soxx': 'SOXX',
            'xlk':  'XLK',
            'gld':  'GLD',
            'uso':  'USO',
            'uup':  'UUP',
            'tnx':  '^TNX',
        }

        log.debug(f"Fetching {len(tickers_needed)} yfinance tickers...")
        series = {}
        for key, ticker in tickers_needed.items():
            series[key] = self._fetch_yf(ticker)

        # VIX
        if not series['vix'].empty:
            raw['vix_level']  = self._last_val(series['vix'])
            raw['vix_z']      = self._rolling_zscore(series['vix'])
            raw['vix_pct52w'] = self._percentile_of_last(series['vix'])
            self._ind_history['vix_level'] = self._series_stats(series['vix'], 'daily')

        # MOVE
        if not series['move'].empty:
            raw['move_level'] = self._last_val(series['move'])
            raw['move_z']     = self._rolling_zscore(series['move'])
            self._ind_history['move_level'] = self._series_stats(series['move'], 'daily')

        # SPY momentum
        if not series['spy'].empty:
            raw['spy_20d'] = self._pct_change_nd(series['spy'], 20)
            raw['spy_5d']  = self._pct_change_nd(series['spy'], 5)
            raw['spy_z']   = self._rolling_zscore(series['spy'].pct_change().dropna() * 100)
            spy_ret20 = series['spy'].pct_change(20).dropna()
            if not spy_ret20.empty:
                self._ind_history['spy_20d'] = self._series_stats(spy_ret20, 'daily')

        # QQQ/SPY ratio
        if not series['qqq'].empty and not series['spy'].empty:
            aligned = pd.concat([series['qqq'], series['spy']], axis=1, join='inner')
            aligned.columns = ['qqq', 'spy']
            ratio = aligned['qqq'] / aligned['spy']
            raw['qqq_spy_ratio'] = self._last_val(ratio)
            raw['qqq_spy_z']     = self._rolling_zscore(ratio)

        # IWM/SPY ratio
        if not series['iwm'].empty and not series['spy'].empty:
            aligned = pd.concat([series['iwm'], series['spy']], axis=1, join='inner')
            aligned.columns = ['iwm', 'spy']
            ratio = aligned['iwm'] / aligned['spy']
            raw['iwm_spy_ratio'] = self._last_val(ratio)
            raw['iwm_spy_z']     = self._rolling_zscore(ratio)

        # XLK/SPY ratio
        if not series['xlk'].empty and not series['spy'].empty:
            aligned = pd.concat([series['xlk'], series['spy']], axis=1, join='inner')
            aligned.columns = ['xlk', 'spy']
            ratio = aligned['xlk'] / aligned['spy']
            raw['xlk_spy_z'] = self._rolling_zscore(ratio)

        # AI/momentum returns
        for key in ('nvda', 'arkk', 'soxx'):
            if not series[key].empty:
                raw[f'{key}_20d']  = self._pct_change_nd(series[key], 20)
                raw[f'{key}_5d']   = self._pct_change_nd(series[key], 5)
                raw[f'{key}_level'] = self._last_val(series[key])
                ret20 = series[key].pct_change(20).dropna()
                if not ret20.empty:
                    self._ind_history[f'{key}_20d'] = self._series_stats(ret20, 'daily')

        # Safe haven
        for key in ('gld', 'uso', 'uup'):
            if not series[key].empty:
                raw[f'{key}_20d']  = self._pct_change_nd(series[key], 20)
                raw[f'{key}_5d']   = self._pct_change_nd(series[key], 5)
                raw[f'{key}_level'] = self._last_val(series[key])
                ret20 = series[key].pct_change(20).dropna()
                if not ret20.empty:
                    self._ind_history[f'{key}_20d'] = self._series_stats(ret20, 'daily')

        # GLD z-score（用于 level 位置判断）
        if not series['gld'].empty:
            raw['gld_z'] = self._rolling_zscore(series['gld'])

        # GLD-SPY 20日滚动相关系数（Baur & Lucey safe haven 触发器）
        if not series['gld'].empty and not series['spy'].empty:
            gld_ret = series['gld'].pct_change().dropna()
            spy_ret = series['spy'].pct_change().dropna()
            corr_aligned = pd.concat([gld_ret, spy_ret], axis=1, join='inner').dropna()
            corr_aligned.columns = ['gld', 'spy']
            if len(corr_aligned) >= 20:
                raw['gld_spy_corr20'] = float(corr_aligned.iloc[-20:].corr().iloc[0, 1])

        # TNX (10yr yield)
        if not series['tnx'].empty:
            raw['tnx_level'] = self._last_val(series['tnx'])
            raw['tnx_z']     = self._rolling_zscore(series['tnx'])

        # HYG (high yield ETF price — proxy if FRED unavailable)
        if not series['hyg'].empty:
            raw['hyg_level'] = self._last_val(series['hyg'])
            raw['hyg_20d']   = self._pct_change_nd(series['hyg'], 20)
            raw['hyg_z']     = self._rolling_zscore(series['hyg'])

        # ── FRED macro indicators ──────────────────────────────────────────
        if self._fred is not None:
            log.debug("Fetching FRED macro indicators...")

            # HY credit spread
            s = self._fetch_fred('BAMLH0A0HYM2')
            if not s.empty:
                raw['hy_spread_level'] = self._last_val(s)
                raw['hy_spread_z']     = self._rolling_zscore(s)
                self._ind_history['hy_spread_level'] = self._series_stats(s, 'daily')

            # IG credit spread
            s = self._fetch_fred('BAMLC0A0CM')
            if not s.empty:
                raw['ig_spread_level'] = self._last_val(s)
                raw['ig_spread_z']     = self._rolling_zscore(s)
                self._ind_history['ig_spread_level'] = self._series_stats(s, 'daily')

            # Yield curve (10yr - 2yr)
            s = self._fetch_fred('T10Y2Y')
            if not s.empty:
                raw['yield_curve_level'] = self._last_val(s)
                raw['yield_curve_z']     = self._rolling_zscore(s)
                self._ind_history['yield_curve_level'] = self._series_stats(s, 'daily')

            # Effective Fed Funds
            s = self._fetch_fred('EFFR')
            if not s.empty:
                raw['effr_level'] = self._last_val(s)
                # Rate change: positive delta = hiking, negative = cutting
                if len(s) >= 252:
                    raw['effr_1y_change'] = float(s.iloc[-1] - s.iloc[-252])
                self._ind_history['effr_level'] = self._series_stats(s, 'daily')

            # Inflation breakeven
            s = self._fetch_fred('T10YIE')
            if not s.empty:
                raw['breakeven_10y_level'] = self._last_val(s)
                raw['breakeven_10y_z']     = self._rolling_zscore(s)
                self._ind_history['breakeven_10y_level'] = self._series_stats(s, 'daily')

            s = self._fetch_fred('T5YIE')
            if not s.empty:
                raw['breakeven_5y_level'] = self._last_val(s)

            # Financial stress index
            s = self._fetch_fred('STLFSI4')
            if not s.empty:
                raw['fin_stress_level'] = self._last_val(s)
                raw['fin_stress_z']     = self._rolling_zscore(s)
                self._ind_history['fin_stress_level'] = self._series_stats(s, 'weekly')

            # NFCI
            s = self._fetch_fred('NFCI')
            if not s.empty:
                raw['nfci_level'] = self._last_val(s)
                self._ind_history['nfci_level'] = self._series_stats(s, 'weekly')

            # Consumer sentiment (monthly, forward-fill)
            s = self._fetch_fred('UMCSENT')
            if not s.empty:
                raw['consumer_sent_level'] = self._last_val(s)
                self._ind_history['consumer_sent_level'] = self._series_stats(s, 'monthly')

            # Recession indicator
            s = self._fetch_fred('USREC')
            if not s.empty:
                raw['recession_flag'] = int(self._last_val(s) or 0)
                self._ind_history['recession_flag'] = self._series_stats(s, 'monthly')

            # Unemployment rate (monthly)
            s = self._fetch_fred('UNRATE')
            if not s.empty:
                raw['unrate_level'] = self._last_val(s)
                self._ind_history['unrate_level'] = self._series_stats(s, 'monthly')

            # Nonfarm payrolls MoM change (monthly)
            s = self._fetch_fred('PAYEMS')
            if not s.empty:
                raw['payems_mom'] = float(s.diff().iloc[-1]) if len(s) >= 2 else None
                self._ind_history['payems_mom'] = self._series_stats(s.diff().dropna(), 'monthly')

            # Initial jobless claims (weekly, thousands)
            s = self._fetch_fred('ICSA')
            if not s.empty:
                raw['icsa_level'] = self._last_val(s)
                self._ind_history['icsa_level'] = self._series_stats(s, 'weekly')

            # Continuing claims (weekly, thousands)
            s = self._fetch_fred('CCSA')
            if not s.empty:
                raw['ccsa_level'] = self._last_val(s)
                self._ind_history['ccsa_level'] = self._series_stats(s, 'weekly')

            # DGS10 / DGS2 daily
            s10 = self._fetch_fred('DGS10')
            s2  = self._fetch_fred('DGS2')
            if not s10.empty and not s2.empty:
                aligned = pd.concat([s10, s2], axis=1, join='inner').dropna()
                if not aligned.empty:
                    aligned.columns = ['dgs10', 'dgs2']
                    raw['dgs10_level'] = float(aligned['dgs10'].iloc[-1])
                    raw['dgs2_level']  = float(aligned['dgs2'].iloc[-1])
                    curve = aligned['dgs10'] - aligned['dgs2']
                    raw['yield_curve_daily'] = float(curve.iloc[-1])
                    raw['yield_curve_daily_z'] = self._rolling_zscore(curve)

        return raw

    def _percentile_of_last(self, s: pd.Series, window: int | None = None) -> float | None:
        """Return percentile rank of last value within rolling window."""
        window = window or self.lookback_days
        if len(s) < 10:
            return None
        w = min(window, len(s))
        arr = s.iloc[-w:].values
        last = float(s.iloc[-1])
        return float(np.mean(arr <= last))

    def _build_rationale(self, raw: dict, cat_scores: dict, score: float,
                         mrpt_w: float, mtfs_w: float) -> str:
        lines = []
        lines.append(f"Regime score: {score:.1f}/100  →  MRPT {mrpt_w:.0%}  MTFS {mtfs_w:.0%}")
        lines.append("")

        vix = raw.get('vix_level')
        vix_z = raw.get('vix_z')
        if vix:
            lines.append(f"  Volatility   : VIX={vix:.1f} (z={vix_z:+.2f})" if vix_z else f"  Volatility   : VIX={vix:.1f}")

        hy = raw.get('hy_spread_level')
        if hy:
            lines.append(f"  Credit       : HY spread={hy:.2f}% IG={raw.get('ig_spread_level', 'n/a')}")

        yc = raw.get('yield_curve_level') or raw.get('yield_curve_daily')
        if yc is not None:
            lines.append(f"  Rates        : Yield curve (10y-2y)={yc:+.2f}  EFFR={raw.get('effr_level', 'n/a')}")

        nvda = raw.get('nvda_20d')
        arkk = raw.get('arkk_20d')
        if nvda is not None:
            lines.append(f"  AI/Momentum  : NVDA 20d={nvda:+.1%}  ARKK 20d={arkk:+.1%}" if arkk else f"  AI/Momentum  : NVDA 20d={nvda:+.1%}")

        fs = raw.get('fin_stress_level')
        if fs is not None:
            lines.append(f"  Macro stress : St.Louis FSI={fs:.4f}  NFCI={raw.get('nfci_level', 'n/a')}")

        gld = raw.get('gld_20d')
        uso = raw.get('uso_20d')
        if gld is not None:
            lines.append(f"  Geopolitical : GLD 20d={gld:+.1%}  USO 20d={uso:+.1%}" if uso else f"  Geopolitical : GLD 20d={gld:+.1%}")

        lines.append("")
        lines.append("  Category scores (0=MRPT, 1=MTFS):")
        for cat, s in cat_scores.items():
            bar = '█' * int(s * 20) + '░' * (20 - int(s * 20))
            lines.append(f"    {cat:15s}: [{bar}] {s:.2f}")

        return '\n'.join(lines)

    def print_report(self, result: dict | None = None):
        """Pretty-print the full regime report."""
        if result is None:
            result = self.detect()

        print()
        print("=" * 70)
        print(f"  MARKET REGIME REPORT  —  {result['as_of']}")
        print("=" * 70)
        print(result['weight_rationale'])
        print()
        print(f"  → FINAL: regime='{result['regime_label']}'  "
              f"score={result['regime_score']}  "
              f"MRPT={result['mrpt_weight']:.0%}  "
              f"MTFS={result['mtfs_weight']:.0%}")
        print("=" * 70)
        return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse, json

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    parser = argparse.ArgumentParser(description='Market regime detector for MRPT/MTFS allocation')
    parser.add_argument('--fred-key', default=os.getenv('FRED_API_KEY', ''),
                        help='FRED API key (or set FRED_API_KEY env var)')
    parser.add_argument('--mrpt-curve', default=None,
                        help='Path to MRPT OOS equity curve CSV')
    parser.add_argument('--mtfs-curve', default=None,
                        help='Path to MTFS OOS equity curve CSV')
    parser.add_argument('--json', action='store_true',
                        help='Output full result as JSON')
    parser.add_argument('--min-weight', type=float, default=0.20,
                        help='Minimum allocation to either strategy (default 0.20)')
    args = parser.parse_args()

    # Auto-find latest OOS curves if not provided
    import glob as _glob

    def _latest(pattern):
        files = _glob.glob(pattern)
        return sorted(files)[-1] if files else None

    mrpt_curve = args.mrpt_curve or _latest(
        os.path.join(BASE_DIR, 'historical_runs/walk_forward/oos_equity_curve_*.csv'))
    mtfs_curve = args.mtfs_curve or _latest(
        os.path.join(BASE_DIR, 'historical_runs/walk_forward_mtfs/oos_equity_curve_*.csv'))

    rd = RegimeDetector(
        fred_api_key=args.fred_key,
        min_weight=args.min_weight,
        mrpt_oos_curve=mrpt_curve,
        mtfs_oos_curve=mtfs_curve,
    )

    result = rd.print_report()

    if args.json:
        # Clean non-serializable
        import math

        def _clean(obj):
            if isinstance(obj, float):
                return None if math.isnan(obj) or math.isinf(obj) else round(obj, 6)
            if isinstance(obj, dict):
                return {k: _clean(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_clean(v) for v in obj]
            return obj

        print(json.dumps(_clean(result), indent=2))
