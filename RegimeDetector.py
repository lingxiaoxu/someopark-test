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
        min_weight: float = 0.20,
        mrpt_oos_curve: str | None = None,
        mtfs_oos_curve: str | None = None,
    ):
        self.fred_api_key  = fred_api_key or os.getenv('FRED_API_KEY', '')
        self.lookback_days = lookback_days
        self.min_weight    = min_weight
        self._fred         = None
        self._mrpt_curve   = mrpt_oos_curve
        self._mtfs_curve   = mtfs_oos_curve

        if FREDAPI_AVAILABLE and self.fred_api_key:
            try:
                self._fred = Fred(api_key=self.fred_api_key)
                log.debug("FRED API connected")
            except Exception as e:
                log.warning(f"FRED API init failed: {e}")

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

    def _fetch_fred(self, series_id: str, days_back: int = 400) -> pd.Series:
        """Fetch FRED series. Returns daily/monthly series."""
        if self._fred is None:
            return pd.Series(dtype=float)
        try:
            start = (datetime.today() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            s = self._fred.get_series(series_id, observation_start=start)
            return s.dropna()
        except Exception as e:
            log.warning(f"FRED fetch {series_id} failed: {e}")
            return pd.Series(dtype=float)

    def _last_val(self, s: pd.Series) -> float | None:
        """Return last non-NaN value."""
        if s.empty:
            return None
        return float(s.iloc[-1])

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

    def _score_volatility(self, raw: dict) -> dict:
        """
        VIX + MOVE → vol regime score (0=low_vol, 1=high_vol)
        MRPT 偏好低波动，MTFS 偏好高波动
        """
        scores = {}

        # VIX z-score
        vix_z = raw.get('vix_z')
        if vix_z is not None:
            # z > 1.5 → 极高波动 (score→1 favors MTFS)
            # z < -1   → 极低波动 (score→0 favors MRPT)
            scores['vix'] = float(np.clip((vix_z + 1) / 2.5, 0, 1))

        # VIX absolute level (secondary check)
        vix_lvl = raw.get('vix_level')
        if vix_lvl is not None:
            # <15=0.1, 25=0.5, 35=0.85, >45=1.0
            scores['vix_level'] = float(np.clip((vix_lvl - 12) / 33, 0.05, 1.0))

        # MOVE z-score
        move_z = raw.get('move_z')
        if move_z is not None:
            scores['move'] = float(np.clip((move_z + 0.5) / 2.5, 0, 1))

        return scores  # individual scores, averaged in main

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
        Gold + Oil → 地缘政治风险
        金价飙升 + 油价飙升 → 地缘紧张 → 不确定性 → 动量（趋势）有机会
        """
        scores = {}

        gld_20d = raw.get('gld_20d')
        if gld_20d is not None:
            # 金价涨 → risk-off → 不利于均值回归 → 偏 MTFS
            scores['gold'] = float(np.clip(0.5 + gld_20d * 2.5, 0.1, 0.85))

        uso_20d = raw.get('uso_20d')
        if uso_20d is not None:
            # 油价大涨 → 地缘风险/通胀预期 → MTFS 偏向
            scores['oil'] = float(np.clip(0.5 + uso_20d * 1.5, 0.15, 0.85))

        uup_20d = raw.get('uup_20d')
        if uup_20d is not None:
            # 美元强 → risk-off → MRPT 偏向
            scores['dollar'] = float(np.clip(0.5 - uup_20d * 2.0, 0.15, 0.85))

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
        }

        log.info(f"Regime: {label}  score={regime_score}  MRPT={mrpt_w:.0%}  MTFS={mtfs_w:.0%}")
        return result

    def _fetch_all_indicators(self) -> dict:
        """Fetch all raw indicator values. Returns flat dict."""
        raw = {}

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

        # MOVE
        if not series['move'].empty:
            raw['move_level'] = self._last_val(series['move'])
            raw['move_z']     = self._rolling_zscore(series['move'])

        # SPY momentum
        if not series['spy'].empty:
            raw['spy_20d'] = self._pct_change_nd(series['spy'], 20)
            raw['spy_5d']  = self._pct_change_nd(series['spy'], 5)
            raw['spy_z']   = self._rolling_zscore(series['spy'].pct_change().dropna() * 100)

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

        # Safe haven
        for key in ('gld', 'uso', 'uup'):
            if not series[key].empty:
                raw[f'{key}_20d']  = self._pct_change_nd(series[key], 20)
                raw[f'{key}_level'] = self._last_val(series[key])

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

            # IG credit spread
            s = self._fetch_fred('BAMLC0A0CM')
            if not s.empty:
                raw['ig_spread_level'] = self._last_val(s)
                raw['ig_spread_z']     = self._rolling_zscore(s)

            # Yield curve (10yr - 2yr)
            s = self._fetch_fred('T10Y2Y')
            if not s.empty:
                raw['yield_curve_level'] = self._last_val(s)
                raw['yield_curve_z']     = self._rolling_zscore(s)

            # Effective Fed Funds
            s = self._fetch_fred('EFFR')
            if not s.empty:
                raw['effr_level'] = self._last_val(s)
                # Rate change: positive delta = hiking, negative = cutting
                if len(s) >= 252:
                    raw['effr_1y_change'] = float(s.iloc[-1] - s.iloc[-252])

            # Inflation breakeven
            s = self._fetch_fred('T10YIE')
            if not s.empty:
                raw['breakeven_10y_level'] = self._last_val(s)
                raw['breakeven_10y_z']     = self._rolling_zscore(s)

            s = self._fetch_fred('T5YIE')
            if not s.empty:
                raw['breakeven_5y_level'] = self._last_val(s)

            # Financial stress index
            s = self._fetch_fred('STLFSI4')
            if not s.empty:
                raw['fin_stress_level'] = self._last_val(s)
                raw['fin_stress_z']     = self._rolling_zscore(s)

            # NFCI
            s = self._fetch_fred('NFCI')
            if not s.empty:
                raw['nfci_level'] = self._last_val(s)

            # Consumer sentiment (monthly, forward-fill)
            s = self._fetch_fred('UMCSENT')
            if not s.empty:
                raw['consumer_sent_level'] = self._last_val(s)

            # Recession indicator
            s = self._fetch_fred('USREC')
            if not s.empty:
                raw['recession_flag'] = int(self._last_val(s) or 0)

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
