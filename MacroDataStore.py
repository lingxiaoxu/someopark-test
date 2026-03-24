"""
MacroDataStore.py
-----------------
本地增量存储宏观/波动率指数历史数据，按年分文件储存。
支持 VIX、MOVE 等 yfinance 指数（2006-01-01 起）。

目录结构：
  price_data/macro/
    vix/
      vix_2006.parquet
      vix_2007.parquet
      ...
      vix_2026.parquet        ← 每日增量更新（日线）
    move/
      move_2006.parquet
      ...
    vix_hourly/
      vix_hourly_2026.parquet ← 每日追加7个固定时间点（纽约时间）
    move_hourly/
      move_hourly_2026.parquet

Hourly 数据说明：
  - 每天固定存储7个时间点：纽约时间 9:30–15:30（yfinance hourly 格式）
  - index = UTC datetime（tz-aware），column = close
  - 每次 update_hourly() 去重：若当天这些时间点已存在，不重复写入
  - 用于计算最近3个月的超短期百分位，判断当前值在近期分布中的位置

用法：
  # 初始化（首次拉取全量历史，日线）
  python MacroDataStore.py --init

  # 每日增量更新（日线 + hourly）
  python MacroDataStore.py --update

  # 读取数据用于 RegimeDetector
  from MacroDataStore import MacroDataStore
  store = MacroDataStore()
  vix = store.load('vix')              # 全部日线历史
  vix_2y = store.load('vix', years=2)  # 最近2年日线
  pct = store.percentiles_short('vix') # 最近3个月 hourly 百分位
"""

import argparse
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# ── 配置 ─────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent / 'price_data' / 'macro'
START_DATE = date(2006, 1, 1)    # 初始拉取起点

TICKERS = {
    'vix':  '^VIX',
    'move': '^MOVE',
}

# Hourly 品种：VIX（真正 hourly）+ VXTLT（30年国债波动率，替代 MOVE hourly）
# MOVE 本身每天只有1个数据点，用 ^VXTLT 作为债券波动率的 hourly 代理
TICKERS_HOURLY = {
    'vix':   '^VIX',
    'vxtlt': '^VXTLT',
}

ET = ZoneInfo('America/New_York')
SHORT_TERM_DAYS  = 90                              # 超短期百分位窗口（日历天）

# 每个 hourly 品种独立配置：小时列表 + 分钟偏移
# ^VIX  : yfinance 返回整点时间戳（10:00, 11:00, ..., 16:00 ET）
# ^VXTLT: yfinance 返回 :30 时间戳（9:30, 10:30, ..., 15:30 ET）
HOURLY_SCHEDULE = {
    'vix':   {'hours': [10, 11, 12, 13, 14, 15, 16], 'minute': 0},
    'vxtlt': {'hours': [9, 10, 11, 12, 13, 14, 15],  'minute': 30},
}

# ── 核心类 ────────────────────────────────────────────────────────────────────

class MacroDataStore:
    """按年分文件存储宏观指数日线数据，支持增量更新。"""

    def __init__(self, base_dir: Path | str = BASE_DIR):
        self.base_dir = Path(base_dir)

    def _year_path(self, name: str, year: int) -> Path:
        return self.base_dir / name / f'{name}_{year}.parquet'

    def _load_year(self, name: str, year: int) -> pd.DataFrame:
        """读取单年文件，不存在返回空 DataFrame。"""
        p = self._year_path(name, year)
        if p.exists():
            return pd.read_parquet(p)
        return pd.DataFrame(columns=['date', 'close']).set_index('date')

    def _save_year(self, name: str, year: int, df: pd.DataFrame) -> None:
        """保存单年文件，index=date。"""
        p = self._year_path(name, year)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.sort_index(inplace=True)
        df.to_parquet(p)

    # ── Hourly 内部方法 ───────────────────────────────────────────────────────

    def _hourly_year_path(self, name: str, year: int) -> Path:
        return self.base_dir / f'{name}_hourly' / f'{name}_hourly_{year}.parquet'

    def _load_hourly_year(self, name: str, year: int) -> pd.DataFrame:
        """读取单年 hourly 文件，不存在返回空 DataFrame（index=UTC datetime）。"""
        p = self._hourly_year_path(name, year)
        if p.exists():
            df = pd.read_parquet(p)
            df.index = pd.to_datetime(df.index, utc=True)
            return df
        return pd.DataFrame(columns=['close'])

    def _save_hourly_year(self, name: str, year: int, df: pd.DataFrame) -> None:
        """保存单年 hourly 文件，index=UTC datetime。"""
        p = self._hourly_year_path(name, year)
        p.parent.mkdir(parents=True, exist_ok=True)
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df.sort_index(inplace=True)
        df.to_parquet(p)

    def _fetch_yf_hourly(self, ticker: str, days_back: int = 7,
                         hours: list[int] | None = None,
                         minute: int = 0) -> pd.DataFrame:
        """
        从 yfinance 拉取最近 days_back 天的 1h 数据（Close），返回 UTC datetime-indexed DataFrame。

        hours  : 只保留纽约时间这些小时（如 [10,11,12,13,14,15,16] 对应 VIX 整点）
        minute : 只保留该分钟偏移（VIX=0 对应 :00，VXTLT=30 对应 :30）
        """
        try:
            raw = yf.download(
                ticker,
                period=f'{days_back}d',
                interval='1h',
                progress=False,
                auto_adjust=True,
            )
            if raw.empty:
                return pd.DataFrame(columns=['close'])
            close = raw['Close']
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = close.dropna()
            if close.empty:
                return pd.DataFrame(columns=['close'])

            idx = pd.to_datetime(raw.index, utc=True)

            if hours is not None:
                idx_et = idx.tz_convert(ET)
                mask = idx_et.hour.isin(hours) & (idx_et.minute == minute)
                close_filtered = close.values[mask]
                idx_filtered   = idx[mask]
            else:
                close_filtered = close.values
                idx_filtered   = idx

            if len(close_filtered) == 0:
                return pd.DataFrame(columns=['close'])

            df = pd.DataFrame({'close': close_filtered}, index=idx_filtered)
            df.index.name = 'datetime'
            return df
        except Exception as e:
            log.warning(f"yfinance hourly fetch {ticker} failed: {e}")
            return pd.DataFrame(columns=['close'])

    # ── Daily 内部方法 ────────────────────────────────────────────────────────

    def _fetch_yf(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """从 yfinance 拉取 [start, end] 的 Close，返回 date-indexed DataFrame。"""
        try:
            raw = yf.download(
                ticker,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                progress=False,
                auto_adjust=True,
            )
            if raw.empty:
                return pd.DataFrame(columns=['close'])
            close = raw['Close']
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = close.dropna()
            if close.empty:
                return pd.DataFrame(columns=['close'])
            df = pd.DataFrame({'close': close})
            df.index = pd.to_datetime(df.index).date
            df.index.name = 'date'
            return df
        except Exception as e:
            log.warning(f"yfinance fetch {ticker} failed: {e}")
            return pd.DataFrame(columns=['close'])

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    def init(self) -> None:
        """首次全量拉取：START_DATE 到今天，按年分文件写入。"""
        today = date.today()
        for name, ticker in TICKERS.items():
            print(f"[init] {name} ({ticker})  {START_DATE} → {today}")
            df = self._fetch_yf(ticker, START_DATE, today)
            if df.empty:
                print(f"  ⚠  no data returned")
                continue
            # 按年拆分写入
            df.index = pd.to_datetime(df.index)
            for year, grp in df.groupby(df.index.year):
                grp.index = grp.index.date
                self._save_year(name, year, grp)
                print(f"  ✓  {name}_{year}.parquet  ({len(grp)} rows)")

    def update(self) -> None:
        """增量更新：日线增量 + hourly 追加（7个固定整点）。"""
        self._update_daily()
        self.update_hourly()

    def _update_daily(self) -> None:
        """增量更新日线：找到每个品种的最新日期，只拉缺失的新数据，追加到当年文件。"""
        today = date.today()
        for name, ticker in TICKERS.items():
            cur_year = today.year
            df_cur   = self._load_year(name, cur_year)

            if df_cur.empty:
                # 当年文件不存在，从年初开始拉
                fetch_start = date(cur_year, 1, 1)
            else:
                last_date = max(df_cur.index) if hasattr(df_cur.index[0], 'year') else \
                            max(pd.to_datetime(df_cur.index).date)
                fetch_start = last_date + timedelta(days=1)

            if fetch_start > today:
                print(f"[update] {name}: already up-to-date ({today})")
                continue

            print(f"[update] {name} ({ticker})  {fetch_start} → {today}")
            new_df = self._fetch_yf(ticker, fetch_start, today)

            if new_df.empty:
                print(f"  ⚠  no new data")
                continue

            # 可能跨年（fetch_start 在去年末，今天是新年）
            new_df.index = pd.to_datetime(new_df.index)
            for year, grp in new_df.groupby(new_df.index.year):
                grp.index = grp.index.date
                existing = self._load_year(name, year)
                if not existing.empty:
                    existing.index = pd.to_datetime(existing.index).date
                    combined = pd.concat([existing, grp])
                    combined = combined[~combined.index.duplicated(keep='last')]
                else:
                    combined = grp
                self._save_year(name, year, combined)
                print(f"  ✓  {name}_{year}.parquet  +{len(grp)} rows  total={len(combined)}")

    def load(self, name: str, years: int | None = None) -> pd.Series:
        """
        读取历史 Close 序列。

        Parameters
        ----------
        name  : 'vix' | 'move'
        years : None=全部历史, 2=最近2年, 等
        """
        today     = date.today()
        cur_year  = today.year

        if years is None:
            year_range = range(START_DATE.year, cur_year + 1)
        else:
            start_year = cur_year - years
            year_range = range(start_year, cur_year + 1)

        frames = []
        for yr in year_range:
            df = self._load_year(name, yr)
            if not df.empty:
                frames.append(df)

        if not frames:
            return pd.Series(dtype=float, name=name)

        combined = pd.concat(frames)
        combined.index = pd.to_datetime(combined.index)
        combined.sort_index(inplace=True)
        combined = combined[~combined.index.duplicated(keep='last')]

        s = combined['close'].dropna()
        s.name = name

        if years is not None:
            cutoff = pd.Timestamp(today) - pd.DateOffset(years=years)
            s = s[s.index >= cutoff]

        return s

    def percentiles(self, name: str) -> dict:
        """
        计算两个版本的百分位数：
          - long_term : 全部历史（2006至今）
          - recent_2y : 最近2年

        返回 dict：
          {
            'long_term': {'p15': float, 'p75': float, 'p85': float, 'median': float, 'n': int},
            'recent_2y': {'p15': float, 'p75': float, 'p85': float, 'median': float, 'n': int},
          }
        """
        result = {}
        for label, years in [('long_term', None), ('recent_2y', 2)]:
            s = self.load(name, years=years)
            if s.empty:
                result[label] = {}
                continue
            result[label] = {
                'p15':    float(np.percentile(s, 15)),
                'p25':    float(np.percentile(s, 25)),
                'median': float(np.percentile(s, 50)),
                'p75':    float(np.percentile(s, 75)),
                'p85':    float(np.percentile(s, 85)),
                'n':      len(s),
            }
        return result

    # ── Hourly 公开接口 ───────────────────────────────────────────────────────

    def update_hourly(self) -> None:
        """
        增量追加 hourly 数据点。
        - 若历史不足 SHORT_TERM_DAYS 天，自动回填（days_back=SHORT_TERM_DAYS）
        - 否则只拉最近7天（补漏），避免重复
        - 已存在的时间点按 UTC datetime 精确去重，不重复写入
        """
        for name, ticker in TICKERS_HOURLY.items():
            sched = HOURLY_SCHEDULE[name]
            # 检测现有数据是否覆盖 SHORT_TERM_DAYS 天
            existing_series = self.load_hourly(name, days=SHORT_TERM_DAYS)
            if existing_series.empty:
                days_back = SHORT_TERM_DAYS
                print(f"[update_hourly] {name}: 无历史数据，回填 {days_back} 天")
            else:
                oldest = existing_series.index.min()
                cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=SHORT_TERM_DAYS)
                if oldest > cutoff + pd.Timedelta(days=3):
                    days_back = SHORT_TERM_DAYS
                    print(f"[update_hourly] {name}: 历史不足，回填 {days_back} 天")
                else:
                    days_back = 7

            sched  = HOURLY_SCHEDULE[name]
            new_df = self._fetch_yf_hourly(ticker, days_back=days_back,
                                           hours=sched['hours'], minute=sched['minute'])
            if new_df.empty:
                print(f"[update_hourly] {name}: no hourly data returned")
                continue

            new_df.index = pd.to_datetime(new_df.index, utc=True)
            added_total = 0

            for year, grp in new_df.groupby(new_df.index.year):
                existing = self._load_hourly_year(name, year)
                if not existing.empty:
                    existing.index = pd.to_datetime(existing.index, utc=True)
                    combined = pd.concat([existing, grp])
                    combined = combined[~combined.index.duplicated(keep='last')]
                    added = len(combined) - len(existing)
                else:
                    combined = grp
                    added = len(grp)
                self._save_hourly_year(name, year, combined)
                added_total += added

            if added_total > 0:
                print(f"[update_hourly] {name}: +{added_total} new hourly points")
            else:
                print(f"[update_hourly] {name}: already up-to-date (no new points)")

    def load_hourly(self, name: str, days: int = SHORT_TERM_DAYS) -> pd.Series:
        """
        读取最近 days 天的 hourly Close 序列（UTC datetime index）。
        默认 90 天。
        """
        cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=days)
        cur_year = datetime.utcnow().year
        # 最多取当年 + 去年（90天不跨超一年）
        frames = []
        for yr in [cur_year - 1, cur_year]:
            df = self._load_hourly_year(name, yr)
            if not df.empty:
                frames.append(df)
        if not frames:
            return pd.Series(dtype=float, name=f'{name}_hourly')

        combined = pd.concat(frames)
        combined.index = pd.to_datetime(combined.index, utc=True)
        combined.sort_index(inplace=True)
        combined = combined[~combined.index.duplicated(keep='last')]
        s = combined['close'].dropna()
        s = s[s.index >= cutoff]
        s.name = f'{name}_hourly'
        return s

    def percentiles_short(self, name: str, days: int = SHORT_TERM_DAYS) -> dict:
        """
        计算超短期（默认90天）hourly 数据的百分位分布。

        返回：
          {
            'p15': float, 'p25': float, 'median': float,
            'p75': float, 'p85': float, 'n': int,
            'current': float,           # 最新一个 hourly close
            'current_pct': float,       # 当前值在近期分布中的百分位 (0-100)
          }
        若数据不足，返回空 dict。
        """
        s = self.load_hourly(name, days=days)
        if len(s) < 10:
            log.warning(f"percentiles_short {name}: only {len(s)} hourly points, skipping")
            return {}
        current = float(s.iloc[-1])
        return {
            'p15':         float(np.percentile(s, 15)),
            'p25':         float(np.percentile(s, 25)),
            'median':      float(np.percentile(s, 50)),
            'p75':         float(np.percentile(s, 75)),
            'p85':         float(np.percentile(s, 85)),
            'n':           len(s),
            'current':     current,
            'current_pct': float((s < current).mean() * 100),  # 当前值排在第几百分位
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')

    parser = argparse.ArgumentParser(description='MacroDataStore — 宏观指数本地增量存储')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--init',   action='store_true', help='全量初始化（2006至今）')
    group.add_argument('--update', action='store_true', help='增量更新（拉取最新）')
    group.add_argument('--info',   action='store_true', help='显示各品种覆盖范围和百分位')
    args = parser.parse_args()

    store = MacroDataStore()

    if args.init:
        store.init()

    elif args.update:
        store.update()

    elif args.info:
        # 日线品种（VIX / MOVE）
        for name in TICKERS:
            s_all = store.load(name)
            s_2y  = store.load(name, years=2)
            pct   = store.percentiles(name)
            print(f"\n{'='*55}")
            print(f"  {name.upper()}  (日线)")
            print(f"{'='*55}")
            if s_all.empty:
                print("  ⚠  no data — run --init first")
                continue
            print(f"  全部历史: {s_all.index[0].date()} → {s_all.index[-1].date()}  ({len(s_all)} rows)")
            print(f"  最近2年 : {s_2y.index[0].date()} → {s_2y.index[-1].date()}  ({len(s_2y)} rows)")
            print(f"\n  长期百分位 (n={pct['long_term']['n']}):")
            lt = pct['long_term']
            print(f"    P15={lt['p15']:.2f}  P25={lt['p25']:.2f}  median={lt['median']:.2f}  P75={lt['p75']:.2f}  P85={lt['p85']:.2f}")
            print(f"\n  近2年百分位 (n={pct['recent_2y']['n']}):")
            r2 = pct['recent_2y']
            print(f"    P15={r2['p15']:.2f}  P25={r2['p25']:.2f}  median={r2['median']:.2f}  P75={r2['p75']:.2f}  P85={r2['p85']:.2f}")
        # Hourly 品种（VIX / VXTLT）
        for name in TICKERS_HOURLY:
            pct_s = store.percentiles_short(name)
            print(f"\n{'='*55}")
            print(f"  {name.upper()}  (近90天 hourly)")
            print(f"{'='*55}")
            if pct_s:
                print(f"  n={pct_s['n']}  P15={pct_s['p15']:.2f}  P25={pct_s['p25']:.2f}  median={pct_s['median']:.2f}  P75={pct_s['p75']:.2f}  P85={pct_s['p85']:.2f}")
                print(f"  当前值={pct_s['current']:.2f}  在近90天中排第 {pct_s['current_pct']:.1f} 百分位")
            else:
                print(f"  ⚠ 无数据（先运行 --update）")


if __name__ == '__main__':
    main()
