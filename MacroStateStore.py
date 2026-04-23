"""
MacroStateStore.py
------------------
日粒度宏观状态快照存储，按周分文件，append-only。

目录结构：
  price_data/macro/state/
    raw/              ← yfinance 原始价格（按 ticker 按年）
      spy_2024.parquet, nvda_2024.parquet, ...
    fred/             ← FRED 原始序列（按系列名按年）
      hy_spread_2024.parquet, yield_curve_2024.parquet, ...
    weekly/           ← 每日完整快照（按周分文件，append-only）
      week_2025-08-25.parquet  ← 该周 Mon 日期命名
      week_2025-09-01.parquet
      ...

weekly parquet 格式：
  index = date (每个交易日一行)
  columns = 每个指标的完整统计：
    {ind}           → 当前值
    {ind}_obs_date  → 实际观测日期（非日频指标可能早于 index date）
    {ind}_prev      → 前一期值
    {ind}_prev_date → 前一期观测日期
    {ind}_chg       → 变化（绝对值）
    {ind}_freq      → 频率 D/W/M
    {ind}_obs30     → 最近 30 obs 均值
    {ind}_vs30      → 当前值 - obs30 均值
    {ind}_obs90     → 最近 90 obs 均值
    {ind}_vs90      → 当前值 - obs90 均值
  对于纯衍生指标（z-score 等）只存单列值。

覆盖的完整指标（对应日报）：
  日频 yfinance: vix, vix3m, vix9d, move, spy_20d/5d, nvda_20d/5d,
                 arkk_20d, soxx_20d, gld_20d, uso_20d, uup_20d,
                 tnx, qqq/iwm/xlk 比率
  日频 FRED    : hy_spread, ig_spread, yield_curve, effr, breakeven_10y
  周频 FRED    : fin_stress, nfci, icsa, ccsa
  月频 FRED    : consumer_sent, recession_flag, unrate, payems
  衍生标量     : vix_z, move_z, hy_spread_z, yield_curve_z,
                 fin_stress_z, effr_yoy, qqq_spy_z, gld_spy_corr20

VIX 变种：从已有的 price_data/macro/vix/ 读取（由 MacroDataStore 维护）

用法：
  python MacroStateStore.py --init          # 全量初始化（2010-至今）
  python MacroStateStore.py --update        # 增量更新今日快照
  python MacroStateStore.py --info          # 显示覆盖范围

  from MacroStateStore import MacroStateStore
  store = MacroStateStore()
  df  = store.load('2025-08-01', '2025-11-01')   # 日期范围 → DataFrame
  v   = store.get('2025-08-27')                   # 单日 → dict (SIMILARITY_FEATURES)
  agg = store.period_vector('2025-07-01', '2025-08-26')  # 区间聚合 → dict
"""

import argparse
import glob
import logging
import os
import time
import warnings


class FredFetchError(Exception):
    """FRED API 网络/服务器错误（区别于「该时段无数据」的空返回）。"""
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# ── 目录 ──────────────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent / 'price_data' / 'macro' / 'state'
VIX_DIR      = Path(__file__).parent / 'price_data' / 'macro' / 'vix'
MOVE_DIR     = Path(__file__).parent / 'price_data' / 'macro' / 'move'
START_DATE   = date(2010, 1, 1)
Z_WINDOW     = 252
Z_MIN        = 60

# ── 指标配置 ──────────────────────────────────────────────────────────────────

# yfinance 原始价格 ticker（VIX 变种从已有的 price_data/macro/vix/ 读取）
YF_TICKERS = {
    'spy':  'SPY',
    'qqq':  'QQQ',
    'iwm':  'IWM',
    'xlk':  'XLK',
    'nvda': 'NVDA',
    'arkk': 'ARKK',
    'soxx': 'SOXX',
    'gld':  'GLD',
    'uso':  'USO',
    'uup':  'UUP',
    'tnx':  '^TNX',
    'hyg':  'HYG',
}

# FRED 序列：(series_id, natural_freq)
FRED_SERIES = {
    'hy_spread':     ('BAMLH0A0HYM2', 'D'),
    'ig_spread':     ('BAMLC0A0CM',   'D'),
    'yield_curve':   ('T10Y2Y',       'D'),
    'effr':          ('EFFR',         'D'),
    'breakeven_10y': ('T10YIE',       'D'),
    'fin_stress':    ('STLFSI4',      'W'),
    'nfci':          ('NFCI',         'W'),
    'icsa':          ('ICSA',         'W'),
    'ccsa':          ('CCSA',         'W'),
    'consumer_sent': ('UMCSENT',      'M'),
    'recession_flag':('USREC',        'M'),
    'unrate':        ('UNRATE',       'M'),
    'payems':        ('PAYEMS',       'M'),
}

# Point-in-Time 发布滞后（天数）
# FRED 的 index date 是观测期，但数据实际在之后才发布。
# 回填历史快照时，对 as_of_date 减去这个天数才是当天真正可见的数据截止日。
#
# 来源：
#   日频市场数据（HY/IG/yield/EFFR/breakeven）：FRED 1个工作日滞后，保守用 1 天
#   STLFSI4：每周五发布上周四数据，约 1-2 天
#   NFCI：每周三发布上周五数据，约 5 天
#   ICSA：每周四发布上周六结束数据，约 5 天
#   CCSA：同一个周四发布比 ICSA 早一周的续领数据，约 12 天
#   UNRATE/PAYEMS：当月月初日期标注，下月第一个周五发布，保守 35 天
#   UMCSENT：月初标注，当月最后一个周五发布初值，保守 25 天
#   USREC：FRED 的 USREC 序列基于 NBER 月度分类，实际更新滞后约 45-60 天
#          （不同于 NBER 官方衰退声明，后者滞后数月至 1 年以上）
FRED_RELEASE_LAG: dict[str, int] = {
    'hy_spread':     1,
    'ig_spread':     1,
    'yield_curve':   1,
    'effr':          1,
    'breakeven_10y': 1,
    'fin_stress':    2,
    'nfci':          5,
    'icsa':          5,
    'ccsa':          12,
    'consumer_sent': 25,
    'recession_flag': 45,
    'unrate':        35,
    'payems':        35,
}

# 需要完整统计（val/prev/chg/obs30/obs90）的指标列表
#   key → (series_type, freq)
#   series_type: 'yf_raw' = 原始价格, 'yf_ret_Nd' = N日收益率, 'fred' = FRED
STAT_INDICATORS = {
    # VIX 系列（从 MacroDataStore 读取）
    'vix':       ('vix_raw',   'D'),
    'vix3m':     ('vix_raw',   'D'),
    'vix9d':     ('vix_raw',   'D'),
    'move':      ('move_raw',  'D'),
    # yfinance 衍生收益率
    'spy_20d':   ('yf_ret', 'D'),
    'spy_5d':    ('yf_ret', 'D'),
    'nvda_20d':  ('yf_ret', 'D'),
    'nvda_5d':   ('yf_ret', 'D'),
    'arkk_20d':  ('yf_ret', 'D'),
    'soxx_20d':  ('yf_ret', 'D'),
    'gld_20d':   ('yf_ret', 'D'),
    'uso_20d':   ('yf_ret', 'D'),
    'uup_20d':   ('yf_ret', 'D'),
    'tnx':       ('yf_raw', 'D'),
    # FRED
    'hy_spread':     ('fred', 'D'),
    'ig_spread':     ('fred', 'D'),
    'yield_curve':   ('fred', 'D'),
    'effr':          ('fred', 'D'),
    'breakeven_10y': ('fred', 'D'),
    'fin_stress':    ('fred', 'W'),
    'nfci':          ('fred', 'W'),
    'icsa':          ('fred', 'W'),
    'ccsa':          ('fred', 'W'),
    'consumer_sent': ('fred', 'M'),
    'recession_flag':('fred', 'M'),
    'unrate':        ('fred', 'M'),
    'payems':        ('fred', 'M'),
}

# 纯衍生标量（只存单列，无历史 stats）
SCALAR_INDICATORS = [
    'vix_z',          # VIX 252d rolling z-score
    'move_z',         # MOVE 252d rolling z-score
    'hy_spread_z',
    'yield_curve_z',
    'fin_stress_z',
    'effr_yoy',       # EFFR YoY change
    'qqq_spy_z',      # QQQ/SPY ratio z-score
    'iwm_spy_z',
    'xlk_spy_z',
    'gld_spy_corr20', # GLD-SPY 20d rolling correlation
]

# MCPS 相似度计算用的核心特征（经 F-ratio + 时间趋势 + 三日区分力分析确定，2026-04-21）
SIMILARITY_FEATURES = [
    'fin_stress_z',   # 金融压力 z-score（F=0.95，无趋势，关键压力区分）
    'hy_spread_z',    # 高收益利差 z-score（F=0.86，无趋势，信用条件）
    'xlk_spy_z',      # 科技/成长相对强度（F=3.78，最有效 SIGNAL）
    'vix_z',          # 波动率制度（F=0.64，last30 捕捉高 vol 环境）
    'breakeven_10y',  # 通胀盈亏平衡（F=2.45，日频，通胀预期）
    'consumer_sent',  # 消费者信心（F=4.79，月频，跨周期价值大）
]


# ── 列名生成 ──────────────────────────────────────────────────────────────────

STAT_SUFFIXES = ['', '_obs_date', '_prev', '_prev_date', '_chg',
                 '_freq', '_obs30', '_vs30', '_obs90', '_vs90']

def stat_cols(ind: str) -> list[str]:
    return [f'{ind}{s}' for s in STAT_SUFFIXES]

ALL_SNAPSHOT_COLS = (
    [c for ind in STAT_INDICATORS for c in stat_cols(ind)]
    + SCALAR_INDICATORS
)


# ── 核心类 ────────────────────────────────────────────────────────────────────

class MacroStateStore:
    """
    日粒度宏观状态快照：存储、查询、聚合。

    增量更新逻辑：
      - 首次调用 init() 回填全量历史
      - 之后每日调用 update() 追加新行到当周 parquet
      - 历史 weekly 文件永不重写
    """

    def __init__(self, base_dir: Path | str = BASE_DIR,
                 fred_api_key: str | None = None):
        self.base_dir    = Path(base_dir)
        self._raw_dir    = self.base_dir / 'raw'
        self._fred_dir   = self.base_dir / 'fred'
        self._weekly_dir = self.base_dir / 'weekly'
        for d in (self._raw_dir, self._fred_dir, self._weekly_dir):
            d.mkdir(parents=True, exist_ok=True)

        self._fred_key = fred_api_key or os.getenv('FRED_API_KEY', '')
        self._fred = None
        if self._fred_key:
            try:
                from fredapi import Fred
                self._fred = Fred(api_key=self._fred_key)
            except Exception as e:
                log.warning(f'FRED init failed: {e}')

    # ── 工具：周文件路径 ─────────────────────────────────────────────────────

    @staticmethod
    def _week_monday(d: date) -> date:
        """给定日期所在周的周一。"""
        return d - timedelta(days=d.weekday())

    def _week_path(self, d: date) -> Path:
        monday = self._week_monday(d)
        return self._weekly_dir / f'week_{monday.isoformat()}.parquet'

    # ── 读写 parquet ──────────────────────────────────────────────────────────

    def _load_pq(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_parquet(path)
        df.index = pd.to_datetime(df.index)
        return df

    def _save_pq(self, path: Path, df: pd.DataFrame) -> None:
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        df = df[~df.index.duplicated(keep='last')]
        df.to_parquet(path)

    # ── 抓取原始数据 ──────────────────────────────────────────────────────────

    def _fetch_yf(self, ticker: str, start: date, end: date,
                  retries: int = 3) -> pd.Series:
        for attempt in range(retries):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    raw = yf.download(
                        ticker,
                        start=start.isoformat(),
                        end=(end + timedelta(days=1)).isoformat(),
                        progress=False, auto_adjust=True,
                    )
                if raw.empty:
                    return pd.Series(dtype=float)
                close = raw['Close'].squeeze()
                if isinstance(close, pd.DataFrame):
                    close = close.iloc[:, 0]
                # 新版 yfinance 单日单 ticker 可能 squeeze 成标量 numpy.float64
                if not isinstance(close, pd.Series):
                    close = pd.Series([float(close)], index=[raw.index[-1]])
                close = close.dropna()
                close.index = pd.to_datetime(close.index)
                return close
            except Exception as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    log.warning(f'yf {ticker} retry {attempt+1}/{retries}: {e} (等待{wait}s)')
                    time.sleep(wait)
                    continue
                log.error(f'yf fetch {ticker} ({start}→{end}) 全部重试失败: {e}')
                return pd.Series(dtype=float)
        return pd.Series(dtype=float)

    def _fetch_fred(self, series_id: str, start: date, end: date,
                    retries: int = 3) -> pd.Series:
        """
        下载 FRED 序列。
        - 正常返回（含空 Series）：该时段无数据，调用方静默跳过
        - 抛 FredFetchError：服务器/网络错误，调用方记为失败
        """
        if self._fred is None:
            return pd.Series(dtype=float)
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                s = self._fred.get_series(
                    series_id,
                    observation_start=start.isoformat(),
                    observation_end=end.isoformat(),
                )
                s.index = pd.to_datetime(s.index)
                return s.dropna()          # 正常（可能为空 = 该时段无数据）
            except Exception as e:
                last_exc = e
                retriable = any(x in str(e).lower()
                                for x in ('500', '503', 'timeout', 'connect',
                                          'internal server', 'bad gateway'))
                if attempt < retries - 1 and retriable:
                    wait = 2 ** attempt
                    log.warning(f'FRED {series_id} retry {attempt+1}/{retries}: {e} (等待{wait}s)')
                    time.sleep(wait)
                    continue
                # 重试耗尽或不可重试错误 → 抛出，让调用方记录
                raise FredFetchError(f'{series_id}: {last_exc}') from last_exc
        raise FredFetchError(f'{series_id}: 重试耗尽')

    # ── 加载 raw yfinance 历史（本地 parquet）─────────────────────────────────

    def _load_raw_yf(self, name: str, start_year: int,
                     end_year: int) -> pd.Series:
        """读取本地 raw/name_YYYY.parquet，拼接为 Series。"""
        frames = []
        for yr in range(start_year, end_year + 1):
            p = self._raw_dir / f'{name}_{yr}.parquet'
            df = self._load_pq(p)
            if not df.empty and 'close' in df.columns:
                frames.append(df['close'])
        if not frames:
            return pd.Series(dtype=float, name=name)
        s = pd.concat(frames).sort_index()
        return s[~s.index.duplicated(keep='last')]

    def _load_vix_variant(self, variant: str) -> pd.Series:
        """
        从 price_data/macro/vix/  读取 VIX 变种（vix / vix3m / vix9d / move）。
        列名可能是 'close' 或 ('close', '^VIX3M') 等多层。
        """
        if variant in ('vix', 'vix3m', 'vix9d'):
            pattern = str(VIX_DIR / f'{variant}_*.parquet')
        elif variant == 'move':
            pattern = str(MOVE_DIR / f'move_*.parquet')
        else:
            return pd.Series(dtype=float)

        files = sorted(glob.glob(pattern))
        if not files:
            return pd.Series(dtype=float)

        frames = []
        for f in files:
            df = pd.read_parquet(f)
            df.index = pd.to_datetime(df.index)
            # 处理多层列名
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = ['_'.join(str(c) for c in col).strip('_')
                              for col in df.columns]
            col = [c for c in df.columns if 'close' in str(c).lower()]
            if col:
                frames.append(df[col[0]].rename(variant))
        if not frames:
            return pd.Series(dtype=float)
        s = pd.concat(frames).sort_index()
        return s[~s.index.duplicated(keep='last')]

    def _load_fred_series(self, name: str, start_year: int,
                          end_year: int) -> pd.Series:
        """从本地 fred/name_YYYY.parquet 读取。"""
        frames = []
        for yr in range(start_year, end_year + 1):
            p = self._fred_dir / f'{name}_{yr}.parquet'
            df = self._load_pq(p)
            if not df.empty and 'value' in df.columns:
                frames.append(df['value'])
        if not frames:
            return pd.Series(dtype=float, name=name)
        s = pd.concat(frames).sort_index()
        return s[~s.index.duplicated(keep='last')]

    # ── 块验证 ────────────────────────────────────────────────────────────────

    def _verify_block(self, path: Path, col: str,
                      yr_start: date, yr_end: date,
                      min_fill: float = 0.4) -> tuple[bool, str]:
        """
        验证一个已写入的年份块是否完整可用。
        - 文件存在且可读
        - 包含 col 列
        - 非空，无全 NaN
        - 行数 >= min_fill × 预期交易日数（宽松：月/周频数据行数少是正常的）
        返回 (ok, reason)。
        """
        if not path.exists():
            return False, '文件未生成'
        try:
            df = pd.read_parquet(path)
        except Exception as e:
            return False, f'文件损坏，无法读取: {e}'
        if df.empty:
            return False, '文件为空'
        if col not in df.columns:
            return False, f'缺少列 {col!r}，实际列: {list(df.columns)}'
        s = df[col].dropna()
        if s.empty:
            return False, '所有值均为 NaN'
        # 行数检查：用预期交易日数的宽松下限
        expected_bdays = len(pd.bdate_range(yr_start.isoformat(),
                                             yr_end.isoformat()))
        # 月/周频 FRED 数据行数远少于交易日，所以用极宽松的 0.01 下限
        # 日频数据用 min_fill（默认 0.4，即至少40%的交易日有数据）
        threshold = max(1, int(expected_bdays * min_fill))
        if len(s) < threshold:
            return False, (f'行数不足: {len(s)} 行 < 预期下限 {threshold}'
                           f' ({expected_bdays} 交易日 × {min_fill:.0%})')
        return True, 'ok'

    # ── 增量写入原始数据 ──────────────────────────────────────────────────────

    def _append_raw_yf(self, name: str, new_data: pd.Series) -> None:
        new_data = new_data.copy().rename('close')
        new_data.index = pd.to_datetime(new_data.index)
        for yr, grp in new_data.groupby(new_data.index.year):
            p = self._raw_dir / f'{name}_{yr}.parquet'
            existing = self._load_pq(p)
            if existing.empty:
                self._save_pq(p, grp.to_frame())
            else:
                combined = pd.concat([existing.get('close', existing.iloc[:, 0]), grp])
                combined = combined[~combined.index.duplicated(keep='last')]
                self._save_pq(p, combined.to_frame(name='close'))

    def _append_fred(self, name: str, new_data: pd.Series) -> None:
        new_data = new_data.copy().rename('value')
        new_data.index = pd.to_datetime(new_data.index)
        for yr, grp in new_data.groupby(new_data.index.year):
            p = self._fred_dir / f'{name}_{yr}.parquet'
            existing = self._load_pq(p)
            if existing.empty:
                self._save_pq(p, grp.to_frame())
            else:
                combined = pd.concat([existing.get('value', existing.iloc[:, 0]), grp])
                combined = combined[~combined.index.duplicated(keep='last')]
                self._save_pq(p, combined.to_frame(name='value'))

    # ── 统计计算 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _rolling_z(s: pd.Series, window: int = Z_WINDOW,
                   min_periods: int = Z_MIN) -> pd.Series:
        mu = s.rolling(window, min_periods=min_periods).mean()
        sd = s.rolling(window, min_periods=min_periods).std()
        return (s - mu) / sd.replace(0, np.nan)

    @staticmethod
    def _indicator_stats(name: str, series: pd.Series,
                         as_of: pd.Timestamp, freq: str,
                         release_lag_days: int = 0) -> dict:
        """
        计算单个指标在 as_of 日的完整统计（Point-in-Time 安全）。

        series         : 原始频率序列（日/周/月）
        as_of          : 快照日期
        release_lag_days: FRED 数据的发布滞后天数。
                          过滤时用 as_of - lag 作为截止日，
                          确保只使用快照当天实际已发布的数据。
        """
        cutoff = as_of - pd.Timedelta(days=release_lag_days)
        s = series[series.index <= cutoff].dropna()
        if s.empty:
            return {}

        cur_val  = float(s.iloc[-1])
        cur_date = s.index[-1].date().isoformat()

        row = {
            f'{name}':           cur_val,
            f'{name}_obs_date':  cur_date,
            f'{name}_freq':      freq,
        }

        # 前一期
        if len(s) >= 2:
            row[f'{name}_prev']      = float(s.iloc[-2])
            row[f'{name}_prev_date'] = s.index[-2].date().isoformat()
            row[f'{name}_chg']       = cur_val - float(s.iloc[-2])

        # 30 / 90 obs 均值
        if len(s) >= 3:
            obs30 = float(s.iloc[-30:].mean())
            obs90 = float(s.iloc[-90:].mean())
            row[f'{name}_obs30'] = obs30
            row[f'{name}_vs30']  = cur_val - obs30
            row[f'{name}_obs90'] = obs90
            row[f'{name}_vs90']  = cur_val - obs90

        return row

    # ── 构建单日快照行 ────────────────────────────────────────────────────────

    def _build_snapshot(self, as_of_date: date,
                        prices: dict[str, pd.Series],
                        fred: dict[str, pd.Series]) -> dict:
        """
        给定所有原始序列，计算 as_of_date 的完整快照行 dict。
        prices: {name: Series(DatetimeIndex, float)}
        fred  : {name: Series(DatetimeIndex, float)}
        """
        ts = pd.Timestamp(as_of_date)
        row: dict = {}

        # ── VIX 变种和 MOVE ──────────────────────────────────────────────────
        for vname in ('vix', 'vix3m', 'vix9d', 'move'):
            s = prices.get(vname, pd.Series(dtype=float))
            if not s.empty:
                row.update(self._indicator_stats(vname, s, ts, 'D'))

        # ── yfinance 原始水平（TNX）────────────────────────────────────────
        for name in ('tnx',):
            s = prices.get(name, pd.Series(dtype=float))
            if not s.empty:
                row.update(self._indicator_stats(name, s, ts, 'D'))

        # ── yfinance N日收益率 ──────────────────────────────────────────────
        ret_config = {
            'spy_20d':  ('spy',  20),
            'spy_5d':   ('spy',  5),
            'nvda_20d': ('nvda', 20),
            'nvda_5d':  ('nvda', 5),
            'arkk_20d': ('arkk', 20),
            'soxx_20d': ('soxx', 20),
            'gld_20d':  ('gld',  20),
            'uso_20d':  ('uso',  20),
            'uup_20d':  ('uup',  20),
        }
        for col_name, (ticker, n) in ret_config.items():
            s = prices.get(ticker, pd.Series(dtype=float))
            if s.empty:
                continue
            ret_s = s.pct_change(n) * 100
            ret_s = ret_s.dropna()
            if not ret_s.empty:
                row.update(self._indicator_stats(col_name, ret_s, ts, 'D'))

        # ── FRED 序列（Point-in-Time：按实际发布滞后过滤）───────────────────
        for name, (_, freq) in FRED_SERIES.items():
            s = fred.get(name, pd.Series(dtype=float))
            if s.empty:
                continue
            # payems 存 MoM 变化量（而非水平值），与日报"非农就业变化"对齐
            if name == 'payems':
                s = s.diff().dropna()
            lag = FRED_RELEASE_LAG.get(name, 0)
            row.update(self._indicator_stats(name, s, ts, freq,
                                             release_lag_days=lag))

        # ── 衍生标量 ────────────────────────────────────────────────────────

        # z-scores（252d rolling，从完整序列计算后取当日值）
        def _z_at(s: pd.Series) -> float | None:
            if s.empty or len(s) < Z_MIN:
                return None
            z = self._rolling_z(s)
            z_to = z[z.index <= ts].dropna()
            return float(z_to.iloc[-1]) if not z_to.empty else None

        vix_s  = prices.get('vix',   pd.Series(dtype=float))
        move_s = prices.get('move',  pd.Series(dtype=float))
        # FRED 衍生标量也要应用 PIT lag（z-score 只用 lag 截止的部分数据）
        def _fred_pit(name: str) -> pd.Series:
            s = fred.get(name, pd.Series(dtype=float))
            if s.empty:
                return s
            lag = FRED_RELEASE_LAG.get(name, 0)
            cutoff = ts - pd.Timedelta(days=lag)
            return s[s.index <= cutoff].dropna()

        hy_s   = _fred_pit('hy_spread')
        yc_s   = _fred_pit('yield_curve')
        fs_s   = _fred_pit('fin_stress')
        effr_s = _fred_pit('effr')

        row['vix_z']         = _z_at(vix_s)
        row['move_z']        = _z_at(move_s)
        row['hy_spread_z']   = _z_at(hy_s)
        row['yield_curve_z'] = _z_at(yc_s)
        row['fin_stress_z']  = _z_at(fs_s)

        # EFFR YoY change（PIT：只看截止到 lag 之前的数据）
        if not effr_s.empty and len(effr_s) >= 252:
            if len(effr_s) >= 252:
                row['effr_yoy'] = float(effr_s.iloc[-1] - effr_s.iloc[-252])

        # QQQ/IWM/XLK vs SPY z-scores
        spy_s = prices.get('spy', pd.Series(dtype=float))
        for num_name in ('qqq', 'iwm', 'xlk'):
            num_s = prices.get(num_name, pd.Series(dtype=float))
            if num_s.empty or spy_s.empty:
                continue
            aligned = pd.concat([num_s, spy_s], axis=1, join='inner')
            aligned.columns = ['num', 'spy']
            ratio = aligned['num'] / aligned['spy']
            row[f'{num_name}_spy_z'] = _z_at(ratio)

        # GLD-SPY 20d rolling correlation
        gld_s = prices.get('gld', pd.Series(dtype=float))
        if not gld_s.empty and not spy_s.empty:
            gld_r = gld_s.pct_change().dropna()
            spy_r = spy_s.pct_change().dropna()
            corr  = gld_r.rolling(20, min_periods=10).corr(spy_r)
            corr_to = corr[corr.index <= ts].dropna()
            if not corr_to.empty:
                row['gld_spy_corr20'] = float(corr_to.iloc[-1])

        return row

    # ── Append 单行到当周 parquet ─────────────────────────────────────────────

    def _append_row(self, snapshot_date: date, row: dict) -> None:
        """把一行快照 append 到当周的 parquet（同一日期的旧行会被替换）。"""
        path = self._week_path(snapshot_date)
        ts   = pd.Timestamp(snapshot_date)
        new_df = pd.DataFrame([row], index=[ts])
        new_df.index.name = 'date'

        if path.exists():
            existing = self._load_pq(path)
            existing = existing[existing.index != ts]   # 去掉同日旧行
            combined = pd.concat([existing, new_df])
        else:
            combined = new_df

        self._save_pq(path, combined)

    # ── 内部：加载全量本地历史（用于构建快照）────────────────────────────────

    def _load_all_prices(self, start_year: int, end_year: int) -> dict[str, pd.Series]:
        """加载所有 yfinance 原始价格 + VIX 变种。"""
        prices = {}
        # VIX 变种：从 MacroDataStore vix/ 目录读
        for v in ('vix', 'vix3m', 'vix9d', 'move'):
            prices[v] = self._load_vix_variant(v)
        # 其他 yfinance 品种：从 raw/ 读
        for name in YF_TICKERS:
            prices[name] = self._load_raw_yf(name, start_year, end_year)
        return prices

    def _load_all_fred(self, start_year: int, end_year: int) -> dict[str, pd.Series]:
        return {name: self._load_fred_series(name, start_year, end_year)
                for name in FRED_SERIES}

    # ── 公开：初始化 / 更新 ───────────────────────────────────────────────────

    def init(self, start: date | str = START_DATE,
             end: date | str | None = None,
             clean: bool = False) -> None:
        """
        全量回填历史。
        1. 下载所有 yfinance 价格 → raw/
        2. 下载所有 FRED 序列    → fred/
        3. 逐日构建快照 → weekly/

        end=None 默认到今天。
        clean=True 先清空 raw/fred/weekly 再从头下载。
        注意：为保证 z-score 计算有足够历史（Z_WINDOW=252），
        下载原始数据时会从 start-400d 开始，但快照只写 [start, end] 区间。

        断点续传：yfinance/FRED 按年分块，已有数据自动跳过；
        如中途失败，直接重跑相同命令即可从断点继续。
        """
        import shutil

        if isinstance(start, str):
            start = date.fromisoformat(start)
        if end is None:
            end = date.today()
        elif isinstance(end, str):
            end = date.fromisoformat(end)

        if clean:
            print('[init] --clean: 清空所有原始数据，从头开始...')
            for d in (self._raw_dir, self._fred_dir, self._weekly_dir):
                shutil.rmtree(d, ignore_errors=True)
                d.mkdir(parents=True, exist_ok=True)
            print('[init] ✓ 清空完成\n')

        # z-score 需要足够历史：
        #   日频序列：252 交易日 ≈ 365 日历日
        #   周频序列（STLFSI4/NFCI 等）：Z_MIN=60 周 ≈ 420 日历日
        # 取 600 日历日作为统一回溯窗口，确保周频 z-score 从第一天即可计算
        fetch_start = start - timedelta(days=600)

        # 收集所有失败项，最后统一汇报
        errors: list[str] = []

        # 重启提示（任何地方失败都打印）
        restart_cmd = (f'python MacroStateStore.py --init --start {start}'
                       + (f' --end {end}' if end != date.today() else ''))

        print(f'[init] 回填快照 {start} → {end}  (原始数据从 {fetch_start} 开始)\n')

        # 1. yfinance raw prices（不含 VIX 变种，那些已有）
        # 按年分块下载，已完整覆盖的年份直接跳过，避免大范围一次性拉取失败
        print('[init] 下载 yfinance 价格...')
        for name, ticker in YF_TICKERS.items():
            total_new = 0
            for yr in range(fetch_start.year, end.year + 1):
                yr_start = max(fetch_start, date(yr, 1, 1))
                yr_end   = min(end, date(yr, 12, 31))
                p = self._raw_dir / f'{name}_{yr}.parquet'
                if p.exists():
                    existing = self._load_pq(p)
                    if not existing.empty:
                        last = existing.index.max().date()
                        if yr < end.year and last >= date(yr, 12, 20):
                            continue
                        if yr == end.year and last >= end - timedelta(days=7):
                            continue
                        yr_start = last + timedelta(days=1)
                s = self._fetch_yf(ticker, yr_start, yr_end)
                if s.empty:
                    msg = f'yf {name}({ticker}) {yr}: 无数据或下载失败'
                    errors.append(msg)
                    print(f'  ✗ {msg}')
                else:
                    self._append_raw_yf(name, s)
                    p = self._raw_dir / f'{name}_{yr}.parquet'
                    ok, reason = self._verify_block(p, 'close', yr_start, yr_end)
                    if not ok:
                        msg = f'yf {name}({ticker}) {yr}: 写入验证失败 — {reason}'
                        errors.append(msg)
                        print(f'  ✗ {msg}')
                    else:
                        total_new += len(s)
            print(f'  {name} ({ticker}): +{total_new} 行' if total_new else
                  f'  {name} ({ticker}): 已是最新')

        # 2. FRED（按年分块，跳过已完整覆盖的年份）
        if self._fred is not None:
            print('\n[init] 下载 FRED 数据...')
            for name, (series_id, _) in FRED_SERIES.items():
                total_new = 0
                any_fail  = False
                for yr in range(fetch_start.year, end.year + 1):
                    yr_start = max(fetch_start, date(yr, 1, 1))
                    yr_end   = min(end, date(yr, 12, 31))
                    p = self._fred_dir / f'{name}_{yr}.parquet'
                    if p.exists():
                        existing = self._load_pq(p)
                        if not existing.empty:
                            last = existing.index.max().date()
                            if yr < end.year and last >= date(yr, 12, 1):
                                continue
                            if yr == end.year and last >= end - timedelta(days=14):
                                continue
                            yr_start = last + timedelta(days=1)
                    try:
                        s = self._fetch_fred(series_id, yr_start, yr_end)
                    except FredFetchError as e:
                        msg = f'FRED {name}({series_id}) {yr}: 服务器错误 — {e}'
                        errors.append(msg)
                        any_fail = True
                        continue
                    if s.empty:
                        # 该时段无数据（如历史覆盖不足），静默跳过，不计为失败
                        log.debug(f'FRED {name} {yr}: 无数据（时段可能超出历史覆盖范围）')
                        continue
                    self._append_fred(name, s)
                    p = self._fred_dir / f'{name}_{yr}.parquet'
                    # 月/周频数据行数少，用 1% 下限即可
                    ok, reason = self._verify_block(p, 'value', yr_start, yr_end,
                                                    min_fill=0.01)
                    if not ok:
                        msg = f'FRED {name}({series_id}) {yr}: 写入验证失败 — {reason}'
                        errors.append(msg)
                        any_fail = True
                    else:
                        total_new += len(s)
                if any_fail:
                    print(f'  ✗ {name} ({series_id}): +{total_new} 行，部分年份失败')
                elif total_new:
                    print(f'  {name} ({series_id}): +{total_new} 行')
                else:
                    print(f'  {name} ({series_id}): 已是最新')
        else:
            print('\n[init] 未配置 FRED_API_KEY，跳过 FRED 数据')

        # 3. 逐日构建快照（从已存本地数据读取，无需再次联网）
        print('\n[init] 构建每日快照...')
        try:
            self._rebuild_snapshots(start, end, data_start_year=fetch_start.year)
        except Exception as e:
            errors.append(f'snapshot build 异常: {e}')
            log.error(f'snapshot build 异常: {e}', exc_info=True)

        # ── 最终报告 ─────────────────────────────────────────────────────────
        print()
        if errors:
            print('=' * 60)
            print(f'[init] ⚠  {len(errors)} 个项目失败:')
            for err in errors:
                print(f'  ✗ {err}')
            print()
            print('  → 已完成的数据已持久化，直接重跑以下命令即可断点续传：')
            print(f'    {restart_cmd}')
            print()
            print('  → 如需完全从头开始（清空所有数据）：')
            print(f'    {restart_cmd} --clean')
            print('=' * 60)
        else:
            print('[init] ✓ 全部完成，无失败项')

    def update(self) -> None:
        """
        增量更新：
        0. 更新 VIX/MOVE 原始数据（MacroDataStore，供快照中 vix_z 使用）
        1. 拉取 yfinance/FRED 的缺失新数据
        2. 为今日构建快照，append 到当周文件
        """
        today    = date.today()
        min_year = today.year - 2   # 只需近2年历史供 z-score / obs90

        # 0. 更新 VIX/MOVE（MacroDataStore），确保快照构建时 VIX 是当日数据
        try:
            from MacroDataStore import MacroDataStore as _MDS
            _MDS().update()
        except Exception as e:
            log.warning(f'[update] MacroDataStore update skipped: {e}')

        # 1. 更新 yfinance raw
        print('[update] 更新 yfinance...')
        for name, ticker in YF_TICKERS.items():
            existing = self._load_raw_yf(name, today.year - 1, today.year)
            fetch_start = (existing.index.max().date() + timedelta(days=1)
                           if not existing.empty else date(today.year, 1, 1))
            if fetch_start > today:
                continue
            s = self._fetch_yf(ticker, fetch_start, today)
            if not s.empty:
                self._append_raw_yf(name, s)
                print(f'  {name}: +{len(s)} 行')

        # 2. 更新 FRED
        if self._fred is not None:
            print('[update] 更新 FRED...')
            for name, (series_id, _) in FRED_SERIES.items():
                frames = []
                for yr in (today.year - 1, today.year):
                    p = self._fred_dir / f'{name}_{yr}.parquet'
                    df = self._load_pq(p)
                    if not df.empty:
                        frames.append(df)
                if frames:
                    existing = pd.concat(frames)
                    fetch_start = (existing.index.max().date() + timedelta(days=1))
                else:
                    fetch_start = date(today.year, 1, 1)
                if fetch_start > today:
                    continue
                try:
                    s = self._fetch_fred(series_id, fetch_start, today)
                except FredFetchError as e:
                    log.warning(f'[update] FRED {name}: {e}')
                    continue
                if not s.empty:
                    self._append_fred(name, s)
                    print(f'  {name}: +{len(s)} 行')

        # 3. 构建今日快照
        print(f'[update] 构建今日快照 {today}...')
        prices = self._load_all_prices(min_year, today.year)
        fred   = self._load_all_fred(min_year, today.year)
        row    = self._build_snapshot(today, prices, fred)
        if row:
            self._append_row(today, row)
            print(f'  ✓  已 append 到 {self._week_path(today).name}')
        else:
            print('  ⚠  快照为空（数据不足？）')

    def _rebuild_snapshots(self, start: date, end: date,
                           data_start_year: int | None = None) -> None:
        """
        从本地存储为 [start, end] 内每个交易日构建快照并写入 weekly 文件。
        仅在 init() 时调用。已有快照的日期会被跳过。

        data_start_year: 原始数据的起始年份（即 fetch_start.year），
                         确保 z-score 回溯数据被完整加载。
                         None 时回退到 min(todo_year) - 1。
        """
        # 找出已有快照覆盖的日期
        existing_dates: set[date] = set()
        for p in sorted(self._weekly_dir.glob('week_*.parquet')):
            df = self._load_pq(p)
            if not df.empty:
                existing_dates.update(d.date() for d in df.index)

        # 用 SPY 实际有价格的日期作为交易日列表
        # — 比 pd.bdate_range 准确，自动排除 MLK Day / Presidents Day 等美股假日
        prices_for_dates = self._load_all_prices(
            min(start.year - 1, start.year), end.year
        )
        spy_s = prices_for_dates.get('spy', pd.Series(dtype=float))
        if not spy_s.empty:
            valid = spy_s[
                (spy_s.index.date >= start) & (spy_s.index.date <= end)  # type: ignore[operator]
            ].index
            all_dates_set = set(d.date() for d in valid)
        else:
            # fallback：业务日（不含假日过滤，比较少见）
            log.warning('SPY 价格不存在，回退到 bdate_range（不含假日过滤）')
            all_dates_set = set(
                d.date() for d in pd.bdate_range(start.isoformat(), end.isoformat())
            )
        todo = sorted(all_dates_set - existing_dates)

        if not todo:
            print('  所有快照已存在，跳过。')
            return

        print(f'  需构建 {len(todo)} 个交易日快照...')

        # 加载全量本地历史（一次性，供所有日期复用）
        min_year = min(d.year for d in todo)
        max_year = max(d.year for d in todo)
        # 从 data_start_year 开始加载，确保 z-score 回溯数据（600天=2年）被完整读入
        load_from = data_start_year if data_start_year is not None else min_year - 1
        prices = self._load_all_prices(load_from, max_year)
        fred   = self._load_all_fred(load_from, max_year)
        del prices_for_dates  # 释放临时变量

        # 按周批量写入（同一周的行合并）
        from collections import defaultdict
        weekly_rows: dict[date, list[tuple[date, dict]]] = defaultdict(list)
        for i, d in enumerate(todo):
            row = self._build_snapshot(d, prices, fred)
            if row:
                monday = self._week_monday(d)
                weekly_rows[monday].append((d, row))
            if (i + 1) % 100 == 0 or (i + 1) == len(todo):
                print(f'  进度: {i+1}/{len(todo)}')

        for monday, day_rows in sorted(weekly_rows.items()):
            path = self._week_path(monday)
            new_df = pd.DataFrame(
                [r for _, r in day_rows],
                index=pd.DatetimeIndex([pd.Timestamp(d) for d, _ in day_rows]),
            )
            new_df.index.name = 'date'
            if path.exists():
                existing = self._load_pq(path)
                ts_set = {pd.Timestamp(d) for d, _ in day_rows}
                existing = existing[~existing.index.isin(ts_set)]
                combined = pd.concat([existing, new_df])
            else:
                combined = new_df
            self._save_pq(path, combined)

        print(f'  ✓  完成，共写入 {len(weekly_rows)} 个周文件')

    # ── 公开：查询接口 ────────────────────────────────────────────────────────

    def load(self, start: str | date | None = None,
             end: str | date | None = None) -> pd.DataFrame:
        """
        读取日期范围内的完整快照 DataFrame。
        index = DatetimeIndex, columns = ALL_SNAPSHOT_COLS 子集（有数据的列）
        """
        start_dt = pd.Timestamp(start) if start else None
        end_dt   = pd.Timestamp(end)   if end   else None

        # 找出覆盖该日期范围的所有 weekly 文件
        all_weeks = sorted(self._weekly_dir.glob('week_*.parquet'))
        frames = []
        for p in all_weeks:
            monday = date.fromisoformat(p.stem.replace('week_', ''))
            # 快速过滤：周一 > end 则跳过；周日（monday+6d）< start 则跳过
            if end_dt and pd.Timestamp(monday) > end_dt + pd.Timedelta(days=7):
                continue
            if start_dt and pd.Timestamp(monday + timedelta(days=6)) < start_dt:
                continue
            df = self._load_pq(p)
            if not df.empty:
                frames.append(df)

        if not frames:
            return pd.DataFrame()

        combined = pd.concat(frames).sort_index()
        combined = combined[~combined.index.duplicated(keep='last')]
        if start_dt:
            combined = combined[combined.index >= start_dt]
        if end_dt:
            combined = combined[combined.index <= end_dt]
        return combined

    def get(self, date_str: str | date,
            features: list[str] | None = None) -> dict:
        """
        获取单日原始值 dict。
        features=None → 返回 SIMILARITY_FEATURES。
        若目标日期无数据，自动取最近3个日历日内的前一个交易日。
        """
        if features is None:
            features = SIMILARITY_FEATURES
        dt = pd.Timestamp(date_str)
        path = self._week_path(dt.date())
        # 如果当周文件不存在，往前搜索最多2周
        df = pd.DataFrame()
        for delta in (0, 7, 14):
            p = self._week_path((dt - pd.Timedelta(days=delta)).date())
            df_try = self._load_pq(p)
            if not df_try.empty:
                df = df_try
                break
        if df.empty:
            return {f: None for f in features}

        before = df[df.index <= dt]
        if before.empty:
            return {f: None for f in features}
        row = before.iloc[-1]
        return {f: (float(row[f]) if f in row.index and pd.notna(row[f]) else None)
                for f in features}

    def period_vector(self, start: str | date | None = None,
                      end: str | date | None = None,
                      features: list[str] | None = None,
                      method: str = 'mean',
                      n_trading_days: int | None = None) -> dict:
        """
        区间聚合：对 features 中的各指标取 mean/median/last。
        默认 features = SIMILARITY_FEATURES。

        n_trading_days: 若提供且 start 为 None，则取 end 之前最近 N 个交易日的数据。
                        例：period_vector(end='2024-06-30', n_trading_days=30)
                        → IS 期最后 30 个交易日的均值（MCPS IS 向量）。
        """
        if features is None:
            features = SIMILARITY_FEATURES
        df = self.load(start, end)
        if df.empty:
            return {f: None for f in features}

        if n_trading_days is not None and start is None:
            df = df.tail(n_trading_days)

        # 只取有数据的列
        available = [f for f in features if f in df.columns]
        sub = df[available]

        if method == 'mean':
            agg = sub.mean()
        elif method == 'median':
            agg = sub.median()
        elif method == 'last':
            agg = sub.iloc[-1]
        else:
            raise ValueError(f'Unknown method: {method!r}')

        return {f: (float(agg[f]) if f in agg.index and pd.notna(agg[f]) else None)
                for f in features}

    def info(self) -> None:
        """打印 weekly 文件覆盖概要。"""
        files = sorted(self._weekly_dir.glob('week_*.parquet'))
        if not files:
            print('  无数据，请先运行 --init')
            return
        print(f'\n{"="*60}')
        print('  MacroStateStore — weekly 快照覆盖')
        print(f'{"="*60}')
        total_rows = 0
        for p in files:
            df = self._load_pq(p)
            if df.empty:
                continue
            fill = df[SIMILARITY_FEATURES].notna().mean().mean() * 100 if \
                   all(f in df.columns for f in SIMILARITY_FEATURES) else 0
            print(f'  {p.stem}  {len(df)} 行  sim_fill={fill:.0f}%  '
                  f'({df.index[0].date()} → {df.index[-1].date()})')
            total_rows += len(df)
        print(f'\n  总计: {len(files)} 周文件  {total_rows} 行')


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='MacroStateStore — 宏观状态日粒度存储')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--init',   action='store_true', help='全量初始化（2010至今）')
    group.add_argument('--update', action='store_true', help='增量更新今日快照')
    group.add_argument('--info',   action='store_true', help='显示覆盖范围')
    group.add_argument('--get',    metavar='DATE',      help='单日状态，如 2025-08-27')
    group.add_argument('--period', metavar='S:E',
                       help='区间聚合，如 2025-08-01:2025-10-31')
    parser.add_argument('--start', metavar='DATE', default=None,
                        help='--init 起始日期，如 2024-01-02（默认 2010-01-01）')
    parser.add_argument('--end',   metavar='DATE', default=None,
                        help='--init 结束日期，如 2024-01-07（默认今天）')
    parser.add_argument('--clean', action='store_true',
                        help='--init 配合使用：清空所有数据从头开始')
    args = parser.parse_args()

    store = MacroStateStore()
    if args.init:
        kwargs: dict = {}
        if args.start:
            kwargs['start'] = args.start
        if args.end:
            kwargs['end'] = args.end
        if args.clean:
            kwargs['clean'] = True
        store.init(**kwargs)
    elif args.update:
        store.update()
    elif args.info:
        store.info()
    elif args.get:
        v = store.get(args.get)
        print(f'\n宏观状态 {args.get}:')
        for k, val in v.items():
            print(f'  {k:<22}: {val:+.4f}' if val is not None
                  else f'  {k:<22}: N/A')
    elif args.period:
        s, e = args.period.split(':')
        v = store.period_vector(s, e)
        print(f'\n宏观状态聚合 {s} → {e}:')
        for k, val in v.items():
            print(f'  {k:<22}: {val:+.4f}' if val is not None
                  else f'  {k:<22}: N/A')


if __name__ == '__main__':
    main()
