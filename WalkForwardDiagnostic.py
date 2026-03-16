#!/usr/bin/env python3
"""
WalkForwardDiagnostic.py — Comprehensive IS vs OOS diagnostic for MRPT & MTFS walk-forward.

Produces Excel with sheets:
  1. Executive_Summary    — Macro IS→OOS comparison table + key conclusions
  2. Macro_Regime         — Per-window macro indicators (VIX, MOVE, HY, YC, etc.)
  3. Cross_Corr_IS        — 42-ticker correlation matrix during IS
  4. Cross_Corr_OOS       — 42-ticker correlation matrix during OOS (full period)
  5. Corr_Shift           — IS→OOS correlation change heatmap
  6. Pair_Cointegration    — Engle-Granger cointegration p-values per window
  7. MRPT_Pairs           — Per-pair per-window stats
  8. MTFS_Pairs           — Per-pair per-window stats
  9. Summary_Diagnosis     — Problem classification per pair
  10. Ticker_Overlap       — Ticker concentration risk analysis

Output: historical_runs/wf_diagnostic_<timestamp>.xlsx
"""

import json, os, sys, warnings
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf
from fredapi import Fred
from scipy import stats as scipy_stats

warnings.filterwarnings('ignore')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from PortfolioMRPTRun import load_historical_data  # mongo → polygon → yahoo waterfall

# ── Load environment ─────────────────────────────────────────────────────────
FRED_API_KEY = os.environ.get('FRED_API_KEY', '')

# ── Load pair universes ──────────────────────────────────────────────────────
with open(os.path.join(BASE_DIR, 'pair_universe_mrpt.json')) as f:
    MRPT_PAIRS = json.load(f)
with open(os.path.join(BASE_DIR, 'pair_universe_mtfs.json')) as f:
    MTFS_PAIRS = json.load(f)

# ── Load walk-forward summaries ──────────────────────────────────────────────
def _latest_json(directory, prefix='walk_forward_summary'):
    files = sorted(
        [f for f in os.listdir(directory) if f.startswith(prefix) and f.endswith('.json')],
        key=lambda x: os.path.getmtime(os.path.join(directory, x))
    )
    return os.path.join(directory, files[-1]) if files else None

MRPT_WF_DIR = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward')
MTFS_WF_DIR = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward_mtfs')

with open(_latest_json(MRPT_WF_DIR)) as f:
    MRPT_WF = json.load(f)
with open(_latest_json(MTFS_WF_DIR)) as f:
    MTFS_WF = json.load(f)

# ── Build window definitions ─────────────────────────────────────────────────
def _build_windows(wf_data, strategy):
    windows = []
    w1 = wf_data['windows'][0]
    windows.append({
        'label': f'{strategy}_IS',
        'start': wf_data.get('train_start', w1['train_start']),
        'end': w1['train_end'],
        'type': 'IS'
    })
    for w in wf_data['windows']:
        windows.append({
            'label': f'{strategy}_OOS_W{w["window_idx"]}',
            'start': w['test_start'],
            'end': w['test_end'],
            'type': 'OOS',
            'oos_sharpe': w.get('oos_sharpe'),
            'oos_pnl': w.get('oos_pnl'),
            'n_pairs': w.get('n_selected_pairs'),
        })
    return windows

MRPT_WINDOWS = _build_windows(MRPT_WF, 'MRPT')
MTFS_WINDOWS = _build_windows(MTFS_WF, 'MTFS')

# ── Collect all unique tickers ───────────────────────────────────────────────
def _all_tickers():
    tickers = set()
    for p in MRPT_PAIRS + MTFS_PAIRS:
        tickers.add(p['s1'])
        tickers.add(p['s2'])
    return sorted(tickers)

STOCK_TICKERS = _all_tickers()

# Macro tickers (yfinance)
MACRO_YF = {
    'VIX': '^VIX', 'MOVE': '^MOVE', 'SPY': 'SPY', 'NVDA': 'NVDA',
    'ARKK': 'ARKK', 'SOXX': 'SOXX', 'GLD': 'GLD', 'USO': 'USO',
    'UUP': 'UUP', 'TNX': '^TNX', 'HYG': 'HYG',
}

# FRED series
FRED_SERIES = {
    'HY_Spread': 'BAMLH0A0HYM2', 'IG_Spread': 'BAMLC0A0CM',
    'Yield_Curve_10Y2Y': 'T10Y2Y', 'Fed_Funds_EFFR': 'EFFR',
    'Breakeven_10Y': 'T10YIE', 'Financial_Stress_StL': 'STLFSI4',
    'NFCI': 'NFCI', 'Consumer_Sentiment': 'UMCSENT', 'NBER_Recession': 'USREC',
    'Unemployment_Rate': 'UNRATE', 'Nonfarm_Payrolls': 'PAYEMS',
    'Initial_Claims': 'ICSA', 'Continued_Claims': 'CCSA',
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_stock_prices(tickers, start, end):
    """Fetch daily close prices via load_historical_data (mongo → polygon → yahoo)."""
    print(f"Fetching {len(tickers)} stock tickers via mongo→polygon→yahoo ({start} → {end})...")
    df = load_historical_data(start, end, list(tickers), data_source='mongo')
    # load_historical_data returns MultiIndex (Price, Ticker)
    if isinstance(df.columns, pd.MultiIndex):
        close = df['Close'] if 'Close' in df.columns.get_level_values(0) else df
    else:
        close = df
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close


def fetch_macro_yf(start, end):
    print(f"Fetching macro YF tickers...")
    tickers = list(MACRO_YF.values())
    df = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df = df['Close']
    df.index = pd.to_datetime(df.index).tz_localize(None)
    inv = {v: k for k, v in MACRO_YF.items()}
    df.columns = [inv.get(c, c) for c in df.columns]
    return df


def fetch_macro_fred(start, end):
    if not FRED_API_KEY:
        print("WARNING: No FRED_API_KEY, skipping FRED data")
        return pd.DataFrame()
    print(f"Fetching {len(FRED_SERIES)} FRED series...")
    fred = Fred(api_key=FRED_API_KEY)
    frames = {}
    for name, series_id in FRED_SERIES.items():
        try:
            s = fred.get_series(series_id, observation_start=start, observation_end=end)
            frames[name] = s
        except Exception as e:
            print(f"  FRED {series_id} failed: {e}")
    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# ANALYSIS FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def window_stats_stock(prices_df, s1, s2, start, end):
    mask = (prices_df.index >= pd.Timestamp(start)) & (prices_df.index <= pd.Timestamp(end))
    sub = prices_df.loc[mask, [s1, s2]].dropna()
    nan_result = {
        's1_return': np.nan, 's2_return': np.nan, 'spread_return': np.nan,
        's1_vol': np.nan, 's2_vol': np.nan, 'correlation': np.nan,
        's1_max_dd': np.nan, 's2_max_dd': np.nan,
        'spread_vol': np.nan, 'n_days': len(sub),
        's1_start_price': np.nan, 's1_end_price': np.nan,
        's2_start_price': np.nan, 's2_end_price': np.nan,
        'beta': np.nan, 's1_skew': np.nan, 's2_skew': np.nan,
        's1_kurt': np.nan, 's2_kurt': np.nan,
        'spread_mean_rev_halflife': np.nan,
    }
    if len(sub) < 5:
        return nan_result

    r1 = sub[s1].pct_change().dropna()
    r2 = sub[s2].pct_change().dropna()
    ret1 = (sub[s1].iloc[-1] / sub[s1].iloc[0]) - 1
    ret2 = (sub[s2].iloc[-1] / sub[s2].iloc[0]) - 1
    spread_ret = ret1 - ret2
    vol1 = r1.std() * np.sqrt(252) if len(r1) > 1 else np.nan
    vol2 = r2.std() * np.sqrt(252) if len(r2) > 1 else np.nan
    corr = r1.corr(r2) if len(r1) > 5 else np.nan

    def _max_dd(series):
        peak = series.cummax()
        dd = (series - peak) / peak
        return dd.min()

    spread_daily = r1 - r2
    spread_vol = spread_daily.std() * np.sqrt(252) if len(spread_daily) > 1 else np.nan

    # Beta: s1 regressed on s2
    beta = np.nan
    if len(r1) > 10 and len(r2) > 10:
        aligned = pd.concat([r1, r2], axis=1).dropna()
        if len(aligned) > 10:
            slope, _, _, _, _ = scipy_stats.linregress(aligned.iloc[:, 1], aligned.iloc[:, 0])
            beta = slope

    # Higher moments
    s1_skew = r1.skew() if len(r1) > 10 else np.nan
    s2_skew = r2.skew() if len(r2) > 10 else np.nan
    s1_kurt = r1.kurtosis() if len(r1) > 10 else np.nan
    s2_kurt = r2.kurtosis() if len(r2) > 10 else np.nan

    # Mean-reversion halflife (log-price spread)
    halflife = np.nan
    if len(sub) > 20:
        log_spread = np.log(sub[s1]) - np.log(sub[s2])
        ls = log_spread.values
        delta = np.diff(ls)
        ls_lag = ls[:-1]
        if np.std(ls_lag) > 1e-10:
            slope_hl, _, _, _, _ = scipy_stats.linregress(ls_lag - np.mean(ls_lag), delta)
            if slope_hl < 0:
                halflife = -np.log(2) / slope_hl

    return {
        's1_return': ret1, 's2_return': ret2, 'spread_return': spread_ret,
        's1_vol': vol1, 's2_vol': vol2, 'spread_vol': spread_vol,
        'correlation': corr, 's1_max_dd': _max_dd(sub[s1]), 's2_max_dd': _max_dd(sub[s2]),
        'n_days': len(sub),
        's1_start_price': sub[s1].iloc[0], 's1_end_price': sub[s1].iloc[-1],
        's2_start_price': sub[s2].iloc[0], 's2_end_price': sub[s2].iloc[-1],
        'beta': beta, 's1_skew': s1_skew, 's2_skew': s2_skew,
        's1_kurt': s1_kurt, 's2_kurt': s2_kurt,
        'spread_mean_rev_halflife': halflife,
    }


def window_stats_macro(macro_yf, macro_fred, start, end):
    stats = {}
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if not macro_yf.empty:
        mask = (macro_yf.index >= s) & (macro_yf.index <= e)
        sub = macro_yf.loc[mask]
        if len(sub) > 0:
            for col in sub.columns:
                vals = sub[col].dropna()
                if len(vals) > 0:
                    stats[f'{col}_mean'] = vals.mean()
                    stats[f'{col}_start'] = vals.iloc[0]
                    stats[f'{col}_end'] = vals.iloc[-1]
                    stats[f'{col}_min'] = vals.min()
                    stats[f'{col}_max'] = vals.max()
                    if col in ('SPY', 'NVDA', 'ARKK', 'SOXX', 'GLD', 'USO', 'UUP', 'HYG'):
                        stats[f'{col}_return'] = (vals.iloc[-1] / vals.iloc[0]) - 1 if vals.iloc[0] != 0 else np.nan
    if not macro_fred.empty:
        mask = (macro_fred.index >= s) & (macro_fred.index <= e)
        sub = macro_fred.loc[mask]
        if len(sub) > 0:
            for col in sub.columns:
                vals = sub[col].dropna()
                if len(vals) > 0:
                    stats[f'{col}_mean'] = vals.mean()
                    stats[f'{col}_start'] = vals.iloc[0]
                    stats[f'{col}_end'] = vals.iloc[-1]
                    stats[f'{col}_change'] = vals.iloc[-1] - vals.iloc[0]
                    # For Nonfarm_Payrolls, compute avg monthly change (in thousands)
                    if col == 'Nonfarm_Payrolls' and len(vals) >= 2:
                        monthly_diffs = vals.diff().dropna()
                        if len(monthly_diffs) > 0:
                            stats['Nonfarm_Payrolls_change'] = monthly_diffs.mean()
    return stats


def engle_granger_pvalue(prices_df, s1, s2, start, end):
    """Compute Engle-Granger cointegration p-value."""
    mask = (prices_df.index >= pd.Timestamp(start)) & (prices_df.index <= pd.Timestamp(end))
    sub = prices_df.loc[mask, [s1, s2]].dropna()
    if len(sub) < 15:
        return np.nan
    try:
        from statsmodels.tsa.stattools import coint
        _, pvalue, _ = coint(sub[s1].values, sub[s2].values)
        return pvalue
    except Exception:
        return np.nan


def compute_cross_correlation(prices_df, tickers, start, end):
    """Compute pairwise return correlation matrix for all tickers in a window."""
    mask = (prices_df.index >= pd.Timestamp(start)) & (prices_df.index <= pd.Timestamp(end))
    available = [t for t in tickers if t in prices_df.columns]
    sub = prices_df.loc[mask, available].dropna(how='all')
    returns = sub.pct_change().dropna()
    return returns.corr()


def diagnose_pair(is_stats, oos_stats_list):
    diagnoses = []
    is_corr = is_stats.get('correlation', np.nan)
    is_spread_vol = is_stats.get('spread_vol', np.nan)
    is_beta = is_stats.get('beta', np.nan)
    is_halflife = is_stats.get('spread_mean_rev_halflife', np.nan)

    for i, oos in enumerate(oos_stats_list):
        w_label = f'W{i+1}'
        oos_corr = oos.get('correlation', np.nan)
        oos_spread_vol = oos.get('spread_vol', np.nan)
        oos_beta = oos.get('beta', np.nan)
        oos_halflife = oos.get('spread_mean_rev_halflife', np.nan)
        s1_ret = oos.get('s1_return', np.nan)
        s2_ret = oos.get('s2_return', np.nan)
        s1_dd = oos.get('s1_max_dd', np.nan)
        s2_dd = oos.get('s2_max_dd', np.nan)

        issues = []

        if not np.isnan(is_corr) and not np.isnan(oos_corr):
            if is_corr - oos_corr > 0.3:
                issues.append(f'corr_collapse({is_corr:.2f}→{oos_corr:.2f})')

        if not np.isnan(is_spread_vol) and not np.isnan(oos_spread_vol) and is_spread_vol > 0:
            vol_ratio = oos_spread_vol / is_spread_vol
            if vol_ratio > 2.0:
                issues.append(f'spread_vol_explode({vol_ratio:.1f}x)')

        if not np.isnan(is_beta) and not np.isnan(oos_beta):
            beta_shift = abs(oos_beta - is_beta)
            if beta_shift > 0.5:
                issues.append(f'beta_shift({is_beta:.2f}→{oos_beta:.2f})')

        if not np.isnan(is_halflife) and not np.isnan(oos_halflife):
            if oos_halflife > is_halflife * 3:
                issues.append(f'halflife_expand({is_halflife:.0f}→{oos_halflife:.0f}d)')
        if not np.isnan(is_halflife) and np.isnan(oos_halflife):
            issues.append('mean_rev_lost')

        for leg, ret, dd in [('s1', s1_ret, s1_dd), ('s2', s2_ret, s2_dd)]:
            if not np.isnan(ret) and abs(ret) > 0.15:
                issues.append(f'{leg}_jump({ret:+.1%})')
            if not np.isnan(dd) and dd < -0.15:
                issues.append(f'{leg}_dd({dd:.1%})')

        if not np.isnan(s1_ret) and not np.isnan(s2_ret):
            if abs(s1_ret - s2_ret) > 0.20:
                issues.append(f'diverge({s1_ret - s2_ret:+.1%})')

        if issues:
            diagnoses.append(f'{w_label}: {"; ".join(issues)}')

    return ' | '.join(diagnoses) if diagnoses else 'Stable across all windows'


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTIVE SUMMARY BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_executive_summary(df_macro, df_regime, df_summary, df_coint, all_windows,
                            mrpt_windows, mtfs_windows, corr_is_mrpt, corr_oos_mrpt):
    """Build the executive summary sheet with macro IS→OOS table and conclusions."""
    rows = []

    # ── Section 1: Macro IS→OOS Comparison ────────────────────────────────────
    rows.append({'Section': '═══ 宏观环境变化 (IS → OOS) ═══', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})

    # Gather IS and OOS macro stats for MRPT
    is_macro = df_regime[df_regime['Window'] == 'MRPT_IS'].iloc[0] if len(df_regime[df_regime['Window'] == 'MRPT_IS']) > 0 else None
    oos_macros = df_regime[(df_regime['Window'].str.startswith('MRPT_OOS'))].copy()

    indicators = [
        ('VIX', 'VIX_mean', '波动率指数', lambda is_v, lo, hi: f'IS期间低波动({is_v:.1f})→OOS窗口间大幅波动({lo:.1f}-{hi:.1f})' if hi - lo > 5 else '基本稳定'),
        ('MOVE', 'MOVE_mean', '债券波动指数', lambda is_v, lo, hi: f'IS={is_v:.0f}, OOS={lo:.0f}-{hi:.0f}' + (' 债券波动扩大' if hi > is_v * 1.2 else ' 稳定')),
        ('SPY回报', 'SPY_return', 'S&P 500回报', lambda is_v, lo, hi: f'IS={is_v:+.1%}大牛市→OOS={lo:+.1%}~{hi:+.1%}震荡市' if is_v > 0.15 and lo < 0 else f'IS={is_v:+.1%}→OOS={lo:+.1%}~{hi:+.1%}'),
        ('HY利差', 'HY_Spread_mean', '高收益信用利差', lambda is_v, lo, hi: f'IS={is_v:.2f}%→OOS={lo:.2f}-{hi:.2f}%' + (' 信用收紧' if hi > is_v * 1.1 else ' 基本稳定')),
        ('IG利差', 'IG_Spread_mean', '投资级信用利差', lambda is_v, lo, hi: f'IS={is_v:.2f}%→OOS={lo:.2f}-{hi:.2f}%' + (' 变化' if abs(hi - is_v) / max(is_v, 0.01) > 0.15 else ' 稳定')),
        ('收益率曲线', 'Yield_Curve_10Y2Y_mean', '10Y-2Y', lambda is_v, lo, hi: f'IS={is_v:+.2f}%{"(近倒挂)" if is_v < 0.15 else ""}→OOS={lo:+.2f}~{hi:+.2f}%' + (' 曲线陡峭化' if hi > is_v + 0.3 else '')),
        ('联储基金利率', 'Fed_Funds_EFFR_mean', 'EFFR', lambda is_v, lo, hi: f'IS={is_v:.2f}%→OOS={lo:.2f}-{hi:.2f}%' + (' 降息周期' if hi < is_v else ' 加息' if lo > is_v else ' 稳定')),
        ('通胀预期', 'Breakeven_10Y_mean', '10Y盈亏平衡', lambda is_v, lo, hi: f'IS={is_v:.2f}%→OOS={lo:.2f}-{hi:.2f}%'),
        ('金融压力', 'Financial_Stress_StL_mean', '圣路易斯金融压力', lambda is_v, lo, hi: f'IS={is_v:.3f}→OOS={lo:.3f}~{hi:.3f}' + (' 压力上升' if hi > is_v + 0.5 else ' 宽松')),
        ('NFCI', 'NFCI_mean', '国家金融状况', lambda is_v, lo, hi: f'IS={is_v:.3f}→OOS={lo:.3f}~{hi:.3f}'),
        ('消费者信心', 'Consumer_Sentiment_mean', '密歇根消费者信心', lambda is_v, lo, hi: f'IS={is_v:.1f}→OOS={lo:.1f}-{hi:.1f}' + (' 信心恶化' if hi < is_v * 0.9 else '')),
        ('NVDA回报', 'NVDA_return', 'AI情绪', lambda is_v, lo, hi: f'IS={is_v:+.1%}→OOS={lo:+.1%}~{hi:+.1%}'),
        ('SOXX回报', 'SOXX_return', '半导体', lambda is_v, lo, hi: f'IS={is_v:+.1%}→OOS={lo:+.1%}~{hi:+.1%}'),
        ('黄金回报', 'GLD_return', '避险', lambda is_v, lo, hi: f'IS={is_v:+.1%}→OOS={lo:+.1%}~{hi:+.1%}'),
        ('原油回报', 'USO_return', '地缘/供给', lambda is_v, lo, hi: f'IS={is_v:+.1%}→OOS={lo:+.1%}~{hi:+.1%}'),
        ('美元回报', 'UUP_return', '美元强弱', lambda is_v, lo, hi: f'IS={is_v:+.1%}→OOS={lo:+.1%}~{hi:+.1%}'),
        ('失业率', 'Unemployment_Rate_mean', '劳动力市场', lambda is_v, lo, hi: f'IS={is_v:.1f}%→OOS={lo:.1f}-{hi:.1f}%' + (' 就业恶化' if hi > is_v + 0.3 else ' 稳定')),
        ('非农就业(千人)', 'Nonfarm_Payrolls_change', '就业增长', lambda is_v, lo, hi: f'IS月均={is_v:+.0f}k→OOS={lo:+.0f}k~{hi:+.0f}k'),
        ('初领失业金(万)', 'Initial_Claims_mean', '初领失业金', lambda is_v, lo, hi: f'IS={is_v/1e4:.1f}万→OOS={lo/1e4:.1f}-{hi/1e4:.1f}万' + (' 上升' if hi > is_v * 1.1 else ' 稳定')),
        ('续领失业金(万)', 'Continued_Claims_mean', '续领失业金', lambda is_v, lo, hi: f'IS={is_v/1e4:.1f}万→OOS={lo/1e4:.1f}-{hi/1e4:.1f}万' + (' 偏高' if hi > is_v * 1.05 else ' 稳定')),
    ]

    for name, col, desc, fmt_fn in indicators:
        is_val = is_macro[col] if is_macro is not None and col in is_macro.index and pd.notna(is_macro[col]) else np.nan
        oos_vals = oos_macros[col].dropna().values if col in oos_macros.columns else np.array([])
        if np.isnan(is_val) or len(oos_vals) == 0:
            rows.append({'Section': '', 'Indicator': name, 'IS_Period': 'N/A', 'OOS_Range': 'N/A', 'Change': '', 'Impact': desc})
            continue
        lo, hi = np.min(oos_vals), np.max(oos_vals)
        change_text = fmt_fn(is_val, lo, hi)
        rows.append({
            'Section': '', 'Indicator': name,
            'IS_Period': f'{is_val:.4g}',
            'OOS_Range': f'{lo:.4g} ~ {hi:.4g}',
            'Change': change_text,
            'Impact': desc,
        })

    # ── Section 2: OOS Window Performance ─────────────────────────────────────
    rows.append({'Section': '', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
    rows.append({'Section': '═══ MRPT OOS窗口表现 ═══', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
    for w in mrpt_windows:
        if w['type'] == 'OOS':
            vix_col = 'VIX_mean'
            spy_col = 'SPY_return'
            wrow = df_regime[df_regime['Window'] == w['label']]
            vix_v = wrow[vix_col].values[0] if len(wrow) > 0 and vix_col in wrow.columns else np.nan
            spy_v = wrow[spy_col].values[0] if len(wrow) > 0 and spy_col in wrow.columns else np.nan
            rows.append({
                'Section': '', 'Indicator': w['label'],
                'IS_Period': f'{w["start"]} → {w["end"]}',
                'OOS_Range': f'Sharpe={w.get("oos_sharpe", "N/A"):.2f}  PnL=${w.get("oos_pnl", 0):+,.0f}' if pd.notna(w.get('oos_sharpe')) else 'N/A',
                'Change': f'VIX={vix_v:.1f}  SPY={spy_v:+.1%}' if not np.isnan(vix_v) else '',
                'Impact': '正收益' if w.get('oos_pnl', 0) and w['oos_pnl'] > 0 else '亏损',
            })

    rows.append({'Section': '', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
    rows.append({'Section': '═══ MTFS OOS窗口表现 ═══', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
    for w in mtfs_windows:
        if w['type'] == 'OOS':
            wrow = df_regime[df_regime['Window'] == w['label']]
            vix_v = wrow['VIX_mean'].values[0] if len(wrow) > 0 and 'VIX_mean' in wrow.columns else np.nan
            spy_v = wrow['SPY_return'].values[0] if len(wrow) > 0 and 'SPY_return' in wrow.columns else np.nan
            rows.append({
                'Section': '', 'Indicator': w['label'],
                'IS_Period': f'{w["start"]} → {w["end"]}',
                'OOS_Range': f'Sharpe={w.get("oos_sharpe", "N/A"):.2f}  PnL=${w.get("oos_pnl", 0):+,.0f}' if pd.notna(w.get('oos_sharpe')) else 'N/A',
                'Change': f'VIX={vix_v:.1f}  SPY={spy_v:+.1%}' if not np.isnan(vix_v) else '',
                'Impact': '正收益' if w.get('oos_pnl', 0) and w['oos_pnl'] > 0 else '亏损',
            })

    # ── Section 3: Problem Summary ────────────────────────────────────────────
    rows.append({'Section': '', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
    rows.append({'Section': '═══ 问题配对分类统计 ═══', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})

    for strategy in ['MRPT', 'MTFS']:
        sdf = df_summary[df_summary['Strategy'] == strategy]
        n_total = len(sdf)
        n_corr_decay = len(sdf[sdf['Issues'].str.contains('Correlation Decay', na=False)])
        n_vol_expand = len(sdf[sdf['Issues'].str.contains('Spread Vol', na=False)])
        n_regime = len(sdf[sdf['Issues'].str.contains('Regime', na=False)])
        n_jump = len(sdf[sdf['Issues'].str.contains('Price Jump', na=False)])
        n_stable = len(sdf[sdf['Issues'] == 'Stable'])

        rows.append({'Section': '', 'Indicator': f'{strategy} ({n_total} pairs)', 'IS_Period': '',
                     'OOS_Range': f'Correlation Decay: {n_corr_decay}', 'Change': f'Spread Vol Expansion: {n_vol_expand}',
                     'Impact': f'Regime Sensitivity: {n_regime} | Price Jump: {n_jump} | Stable: {n_stable}'})

    # ── Section 4: Key Conclusions ────────────────────────────────────────────
    rows.append({'Section': '', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
    rows.append({'Section': '═══ 综合分析结论 ═══', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})

    conclusions = [
        ('1. 过拟合', '根本原因', 'IS期间(2024.02-2025.07)是连续牛市(SPY+29~33%)，所有pair的correlation在trending市场中被系统性抬高。IS训练出来的参数在OOS震荡市(SPY±5%)失效。'),
        ('2. Regime变化', '宏观环境', '收益率曲线从IS近倒挂(+0.08%)→OOS正常陡峭(+0.53~0.69%)；VIX从IS均值17.6→OOS波动15-22；联储从紧缩转降息。整个利率/波动率体制发生了结构性变化。'),
        ('3. Correlation崩塌', '配对关系', 'IS期间牛市环境下pair之间的correlation被人为抬高。OOS市场震荡后，pair关系瓦解。MTFS受害更深(BK/ALL 0.39→0.04, ETR/AVB 0.38→-0.04)，因为动量策略更依赖方向一致性。'),
        ('4. 单腿价格跳跃', '集中风险', 'CL(Colgate)在W5暴涨+22.3%导致3个MRPT pair连锁亏损；WST在W3/W5深跌-16~18%影响3个pair；GRMN在W3暴跌-23.5%。Ticker重叠放大了单一事件的冲击。'),
        ('5. MTFS特有问题', '策略缺陷', 'MTFS W1(2025.07-08)暴亏-$28k，因为IS学到的"动量方向"在新窗口完全反转。W6(2026.02-03)再次暴亏-$21k，美股大跌(SPY-3.4%)触发系统性动量反转。MTFS对market regime敏感度远高于MRPT。'),
        ('6. 利息侵蚀', '成本结构', 'MRPT Gross PnL +$16.5k被利息-$9.9k吃掉60%；MTFS利息-$10.7k在亏损上雪上加霜。$500k本金的margin成本年化约2.7%，在Sharpe<0.5时几乎无法覆盖。'),
    ]
    for title, category, detail in conclusions:
        rows.append({'Section': '', 'Indicator': title, 'IS_Period': category, 'OOS_Range': '', 'Change': detail, 'Impact': ''})

    # ── Section 5: Improvement Recommendations ────────────────────────────────
    rows.append({'Section': '', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
    rows.append({'Section': '═══ 改进建议 ═══', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})

    recs = [
        ('短期: Pair级止损', '实现', '单pair OOS亏损>$5k或>1%时暂停→GRMN/KO、BK/ALL可及时止血'),
        ('短期: 降低Ticker重叠', '实现', '限制每个ticker最多出现在2个pair→CL(3对)、WST(3对)的连锁风险消除'),
        ('短期: Top-3 Ensemble', '实现', '不只选DSR最高1个param，用Top-3加权平均信号→降低单一param过拟合'),
        ('中期: Rolling协整门槛', '实现', '近20天pair协整p-value>0.3或correlation<0.1时暂停该pair'),
        ('中期: Regime-aware参数', '设计', 'VIX高→conservative param，VIX低→aggressive param，动态切换'),
        ('长期: ML Pair Scorer', '研究', '用IS Sharpe、DSR、correlation变化、sector集中度预测OOS盈利概率'),
    ]
    for title, status, detail in recs:
        rows.append({'Section': '', 'Indicator': title, 'IS_Period': status, 'OOS_Range': '', 'Change': detail, 'Impact': ''})

    # ── Section 6: Daily Report Macro Snapshot ───────────────────────────────
    daily_text = _read_daily_report_macro()
    if daily_text:
        rows.append({'Section': '', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
        rows.append({'Section': '═══ 当前宏观环境快照 (来自 daily_report) ═══', 'Indicator': '', 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})
        for line in daily_text.split('\n'):
            if line.strip():
                rows.append({'Section': '', 'Indicator': line.rstrip(), 'IS_Period': '', 'OOS_Range': '', 'Change': '', 'Impact': ''})

    return pd.DataFrame(rows)


def _read_daily_report_macro():
    """Read the latest daily_report macro section from trading_signals/."""
    import glob as globmod
    pattern = os.path.join(BASE_DIR, 'trading_signals', 'daily_report_*.txt')
    files = sorted(globmod.glob(pattern))
    if not files:
        return None
    latest = files[-1]
    try:
        with open(latest, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        return None

    # Extract macro table: from "指标" header to the empty line after 续领失业金
    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if '指标' in line and '当前值' in line and '前值' in line:
            start_idx = i
        if start_idx is not None and i > start_idx + 2 and line.strip() == '':
            end_idx = i
            break
    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = min(start_idx + 30, len(lines))

    header = f"  来源: {os.path.basename(latest)}\n"
    return header + ''.join(lines[start_idx:end_idx])


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    all_windows = MRPT_WINDOWS + MTFS_WINDOWS
    earliest = min(w['start'] for w in all_windows)
    latest = max(w['end'] for w in all_windows)
    fetch_start = (pd.Timestamp(earliest) - pd.Timedelta(days=60)).strftime('%Y-%m-%d')
    fetch_end = (pd.Timestamp(latest) + pd.Timedelta(days=5)).strftime('%Y-%m-%d')

    print(f"Analysis period: {earliest} → {latest}")
    print(f"Fetch range: {fetch_start} → {fetch_end}")
    print(f"MRPT: {len(MRPT_PAIRS)} pairs | MTFS: {len(MTFS_PAIRS)} pairs")
    print(f"Unique stock tickers: {len(STOCK_TICKERS)}")
    print()

    # ── Fetch data ────────────────────────────────────────────────────────────
    stock_prices = fetch_stock_prices(STOCK_TICKERS, fetch_start, fetch_end)
    macro_yf = fetch_macro_yf(fetch_start, fetch_end)
    macro_fred = fetch_macro_fred(fetch_start, fetch_end)
    print()

    # ══════════════════════════════════════════════════════════════════════════
    # Macro Environment per Window
    # ══════════════════════════════════════════════════════════════════════════
    print("Building macro environment analysis...")
    macro_rows = []
    for w in all_windows:
        row = {
            'Strategy': w['label'].split('_')[0],
            'Window': w['label'], 'Type': w['type'],
            'Start': w['start'], 'End': w['end'],
        }
        if w['type'] == 'OOS':
            row['OOS_Sharpe'] = w.get('oos_sharpe')
            row['OOS_PnL'] = w.get('oos_pnl')
            row['N_Pairs'] = w.get('n_pairs')
        mstats = window_stats_macro(macro_yf, macro_fred, w['start'], w['end'])
        row.update(mstats)
        macro_rows.append(row)
    df_macro = pd.DataFrame(macro_rows)

    # ══════════════════════════════════════════════════════════════════════════
    # Macro Regime Comparison
    # ══════════════════════════════════════════════════════════════════════════
    print("Building regime comparison...")
    key_indicators = [
        'VIX_mean', 'MOVE_mean', 'HY_Spread_mean', 'IG_Spread_mean',
        'Yield_Curve_10Y2Y_mean', 'Fed_Funds_EFFR_mean', 'Breakeven_10Y_mean',
        'Financial_Stress_StL_mean', 'NFCI_mean', 'Consumer_Sentiment_mean',
        'SPY_return', 'NVDA_return', 'ARKK_return', 'SOXX_return',
        'GLD_return', 'USO_return', 'UUP_return',
        'Unemployment_Rate_mean', 'Nonfarm_Payrolls_change',
        'Initial_Claims_mean', 'Continued_Claims_mean',
    ]
    regime_rows = []
    for _, row in df_macro.iterrows():
        r = {'Window': row['Window'], 'Type': row['Type'], 'Start': row['Start'], 'End': row['End']}
        if 'OOS_Sharpe' in row:
            r['OOS_Sharpe'] = row.get('OOS_Sharpe')
            r['OOS_PnL'] = row.get('OOS_PnL')
        for k in key_indicators:
            r[k] = row.get(k, np.nan)
        regime_rows.append(r)
    df_regime = pd.DataFrame(regime_rows)

    # ══════════════════════════════════════════════════════════════════════════
    # Cross-Correlation Matrices (IS vs OOS)
    # ══════════════════════════════════════════════════════════════════════════
    print("Computing cross-correlation matrices...")
    # Use MRPT IS period for IS correlation, full OOS span for OOS
    mrpt_is = MRPT_WINDOWS[0]
    mrpt_oos_start = MRPT_WINDOWS[1]['start']
    mrpt_oos_end = MRPT_WINDOWS[-1]['end']

    corr_is = compute_cross_correlation(stock_prices, STOCK_TICKERS, mrpt_is['start'], mrpt_is['end'])
    corr_oos = compute_cross_correlation(stock_prices, STOCK_TICKERS, mrpt_oos_start, mrpt_oos_end)

    # Correlation shift
    common_tickers = sorted(set(corr_is.columns) & set(corr_oos.columns))
    corr_shift = corr_oos.loc[common_tickers, common_tickers] - corr_is.loc[common_tickers, common_tickers]

    # ══════════════════════════════════════════════════════════════════════════
    # Per-window Cointegration Tests
    # ══════════════════════════════════════════════════════════════════════════
    print("Running cointegration tests per window...")
    coint_rows = []
    for strategy, pairs, windows in [('MRPT', MRPT_PAIRS, MRPT_WINDOWS), ('MTFS', MTFS_PAIRS, MTFS_WINDOWS)]:
        for p in pairs:
            s1, s2 = p['s1'], p['s2']
            pair_key = f"{s1}/{s2}"
            if s1 not in stock_prices.columns or s2 not in stock_prices.columns:
                continue
            row = {'Strategy': strategy, 'Pair': pair_key, 'Sector': p.get('sector', '')}
            for w in windows:
                pval = engle_granger_pvalue(stock_prices, s1, s2, w['start'], w['end'])
                row[w['label']] = pval
            # IS→OOS change
            is_pval = row.get(f'{strategy}_IS', np.nan)
            oos_pvals = [row.get(f'{strategy}_OOS_W{i}', np.nan) for i in range(1, 7)]
            oos_pvals_clean = [v for v in oos_pvals if not np.isnan(v)]
            row['IS_Coint_pval'] = is_pval
            row['Avg_OOS_Coint_pval'] = np.mean(oos_pvals_clean) if oos_pvals_clean else np.nan
            row['Coint_Deterioration'] = 'YES' if not np.isnan(is_pval) and is_pval < 0.05 and row['Avg_OOS_Coint_pval'] > 0.1 else (
                'MARGINAL' if not np.isnan(is_pval) and is_pval < 0.1 and row.get('Avg_OOS_Coint_pval', 1) > 0.2 else 'NO'
            )
            coint_rows.append(row)
    df_coint = pd.DataFrame(coint_rows)

    # ══════════════════════════════════════════════════════════════════════════
    # Pair-level analysis (MRPT & MTFS)
    # ══════════════════════════════════════════════════════════════════════════
    def build_pair_analysis(pairs, windows, strategy):
        print(f"Building {strategy} pair analysis ({len(pairs)} pairs × {len(windows)} windows)...")
        rows = []
        for p in pairs:
            s1, s2 = p['s1'], p['s2']
            pair_key = f"{s1}/{s2}"
            if s1 not in stock_prices.columns or s2 not in stock_prices.columns:
                print(f"  WARNING: {pair_key} — missing price data")
                continue
            is_stats = None
            oos_stats_list = []
            for w in windows:
                stats = window_stats_stock(stock_prices, s1, s2, w['start'], w['end'])
                row = {
                    'Pair': pair_key, 'S1': s1, 'S2': s2,
                    'Window': w['label'], 'Type': w['type'],
                    'Start': w['start'], 'End': w['end'],
                }
                if w['type'] == 'OOS':
                    row['OOS_Sharpe'] = w.get('oos_sharpe')
                    row['OOS_PnL'] = w.get('oos_pnl')
                    oos_stats_list.append(stats)
                else:
                    is_stats = stats
                row.update({
                    'S1_Return': stats['s1_return'], 'S2_Return': stats['s2_return'],
                    'Spread_Return': stats['spread_return'],
                    'S1_AnnVol': stats['s1_vol'], 'S2_AnnVol': stats['s2_vol'],
                    'Spread_AnnVol': stats['spread_vol'], 'Correlation': stats['correlation'],
                    'Beta': stats['beta'],
                    'S1_Skew': stats['s1_skew'], 'S2_Skew': stats['s2_skew'],
                    'S1_Kurt': stats['s1_kurt'], 'S2_Kurt': stats['s2_kurt'],
                    'Halflife': stats['spread_mean_rev_halflife'],
                    'S1_MaxDD': stats['s1_max_dd'], 'S2_MaxDD': stats['s2_max_dd'],
                    'N_Days': stats['n_days'],
                    'S1_StartPrice': stats['s1_start_price'], 'S1_EndPrice': stats['s1_end_price'],
                    'S2_StartPrice': stats['s2_start_price'], 'S2_EndPrice': stats['s2_end_price'],
                })
                rows.append(row)
            if is_stats and oos_stats_list:
                diag = diagnose_pair(is_stats, oos_stats_list)
                for r in rows:
                    if r['Pair'] == pair_key and r['Type'] == 'IS':
                        r['Diagnosis'] = diag
        return pd.DataFrame(rows)

    df_mrpt = build_pair_analysis(MRPT_PAIRS, MRPT_WINDOWS, 'MRPT')
    df_mtfs = build_pair_analysis(MTFS_PAIRS, MTFS_WINDOWS, 'MTFS')

    # ══════════════════════════════════════════════════════════════════════════
    # Summary Diagnosis
    # ══════════════════════════════════════════════════════════════════════════
    print("Building summary diagnosis...")
    summary_rows = []
    for strategy, df_pairs, pairs, windows in [('MRPT', df_mrpt, MRPT_PAIRS, MRPT_WINDOWS), ('MTFS', df_mtfs, MTFS_PAIRS, MTFS_WINDOWS)]:
        for p in pairs:
            pair_key = f"{p['s1']}/{p['s2']}"
            pair_data = df_pairs[df_pairs['Pair'] == pair_key]
            if pair_data.empty:
                continue
            is_row = pair_data[pair_data['Type'] == 'IS']
            oos_rows = pair_data[pair_data['Type'] == 'OOS']
            is_corr = is_row['Correlation'].values[0] if len(is_row) > 0 else np.nan
            is_spread_vol = is_row['Spread_AnnVol'].values[0] if len(is_row) > 0 else np.nan
            is_beta = is_row['Beta'].values[0] if len(is_row) > 0 else np.nan
            is_halflife = is_row['Halflife'].values[0] if len(is_row) > 0 else np.nan
            oos_corrs = oos_rows['Correlation'].values
            oos_spread_vols = oos_rows['Spread_AnnVol'].values
            oos_betas = oos_rows['Beta'].values
            oos_halflives = oos_rows['Halflife'].values
            avg_oos_corr = np.nanmean(oos_corrs) if len(oos_corrs) > 0 else np.nan
            avg_oos_spread_vol = np.nanmean(oos_spread_vols) if len(oos_spread_vols) > 0 else np.nan
            avg_oos_beta = np.nanmean(oos_betas) if len(oos_betas) > 0 else np.nan
            avg_oos_halflife = np.nanmean(oos_halflives) if len(oos_halflives) > 0 else np.nan

            issues = []
            if not np.isnan(is_corr) and not np.isnan(avg_oos_corr) and is_corr - avg_oos_corr > 0.2:
                issues.append('Correlation Decay')
            if not np.isnan(is_spread_vol) and not np.isnan(avg_oos_spread_vol) and is_spread_vol > 0 and avg_oos_spread_vol / is_spread_vol > 1.5:
                issues.append('Spread Vol Expansion')
            if not np.isnan(is_beta) and not np.isnan(avg_oos_beta) and abs(avg_oos_beta - is_beta) > 0.3:
                issues.append('Beta Instability')
            if not np.isnan(is_halflife) and not np.isnan(avg_oos_halflife) and avg_oos_halflife > is_halflife * 2:
                issues.append('Mean-Rev Slowdown')

            oos_sharpes = oos_rows['OOS_Sharpe'].dropna().values
            if len(oos_sharpes) >= 4 and np.std(oos_sharpes) > 3:
                issues.append('High Regime Sensitivity')
            for leg in ['S1_Return', 'S2_Return']:
                vals = oos_rows[leg].dropna().values
                if len(vals) > 0 and np.any(np.abs(vals) > 0.15):
                    issues.append(f'{leg.split("_")[0]} Price Jump')
                    break

            # Cointegration check
            coint_row = df_coint[(df_coint['Strategy'] == strategy) & (df_coint['Pair'] == pair_key)]
            coint_det = coint_row['Coint_Deterioration'].values[0] if len(coint_row) > 0 else ''
            if coint_det == 'YES':
                issues.append('Cointegration Lost')

            diagnosis = is_row['Diagnosis'].values[0] if len(is_row) > 0 and 'Diagnosis' in is_row.columns and pd.notna(is_row['Diagnosis'].values[0]) else ''

            summary_rows.append({
                'Strategy': strategy, 'Pair': pair_key, 'Sector': p.get('sector', ''),
                'IS_Corr': is_corr, 'OOS_Corr': avg_oos_corr, 'Corr_Change': avg_oos_corr - is_corr if not np.isnan(is_corr) and not np.isnan(avg_oos_corr) else np.nan,
                'IS_SpreadVol': is_spread_vol, 'OOS_SpreadVol': avg_oos_spread_vol,
                'SpreadVol_Ratio': avg_oos_spread_vol / is_spread_vol if not np.isnan(is_spread_vol) and is_spread_vol > 0 else np.nan,
                'IS_Beta': is_beta, 'OOS_Beta': avg_oos_beta,
                'IS_Halflife': is_halflife, 'OOS_Halflife': avg_oos_halflife,
                'IS_Coint_pval': coint_row['IS_Coint_pval'].values[0] if len(coint_row) > 0 else np.nan,
                'OOS_Coint_pval': coint_row['Avg_OOS_Coint_pval'].values[0] if len(coint_row) > 0 else np.nan,
                'Coint_Lost': coint_det,
                'Issues': ', '.join(issues) if issues else 'Stable',
                'Detailed_Diagnosis': diagnosis,
            })
    df_summary = pd.DataFrame(summary_rows)

    # ══════════════════════════════════════════════════════════════════════════
    # Ticker Overlap Analysis
    # ══════════════════════════════════════════════════════════════════════════
    print("Building ticker overlap analysis...")
    ticker_counts = {}
    for strategy, pairs in [('MRPT', MRPT_PAIRS), ('MTFS', MTFS_PAIRS)]:
        for p in pairs:
            for sym in [p['s1'], p['s2']]:
                key = (sym, strategy)
                ticker_counts[key] = ticker_counts.get(key, 0) + 1
    overlap_rows = []
    for (sym, strategy), count in sorted(ticker_counts.items(), key=lambda x: -x[1]):
        if count >= 2:
            # Find which pairs use this ticker
            src = MRPT_PAIRS if strategy == 'MRPT' else MTFS_PAIRS
            involved = [f"{p['s1']}/{p['s2']}" for p in src if p['s1'] == sym or p['s2'] == sym]
            overlap_rows.append({
                'Ticker': sym, 'Strategy': strategy, 'N_Pairs': count,
                'Pairs': ', '.join(involved),
                'Risk': 'HIGH' if count >= 3 else 'MEDIUM',
            })
    df_overlap = pd.DataFrame(overlap_rows) if overlap_rows else pd.DataFrame(columns=['Ticker', 'Strategy', 'N_Pairs', 'Pairs', 'Risk'])

    # ══════════════════════════════════════════════════════════════════════════
    # Executive Summary
    # ══════════════════════════════════════════════════════════════════════════
    print("Building executive summary...")
    df_exec = build_executive_summary(df_macro, df_regime, df_summary, df_coint,
                                       all_windows, MRPT_WINDOWS, MTFS_WINDOWS,
                                       corr_is, corr_oos)

    # ══════════════════════════════════════════════════════════════════════════
    # WRITE EXCEL
    # ══════════════════════════════════════════════════════════════════════════
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(BASE_DIR, 'historical_runs', f'wf_diagnostic_{ts}.xlsx')
    print(f"\nWriting to {out_path}...")

    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        df_exec.to_excel(writer, sheet_name='Executive_Summary', index=False)
        df_regime.to_excel(writer, sheet_name='Macro_Regime', index=False)
        corr_is.to_excel(writer, sheet_name='Cross_Corr_IS')
        corr_oos.to_excel(writer, sheet_name='Cross_Corr_OOS')
        corr_shift.to_excel(writer, sheet_name='Corr_Shift_IS_to_OOS')
        df_coint.to_excel(writer, sheet_name='Pair_Cointegration', index=False)
        df_mrpt.to_excel(writer, sheet_name='MRPT_Pairs', index=False)
        df_mtfs.to_excel(writer, sheet_name='MTFS_Pairs', index=False)
        df_summary.to_excel(writer, sheet_name='Summary_Diagnosis', index=False)
        df_overlap.to_excel(writer, sheet_name='Ticker_Overlap', index=False)
        df_macro.to_excel(writer, sheet_name='Macro_Raw', index=False)
        # Daily report macro snapshot
        daily_report_text = _read_daily_report_macro()
        if daily_report_text:
            df_daily = pd.DataFrame({'Daily_Report_Macro_Snapshot': daily_report_text.split('\n')})
            df_daily.to_excel(writer, sheet_name='Daily_Report_Snapshot', index=False)

    print(f"Done! Output: {out_path}")

    # ══════════════════════════════════════════════════════════════════════════
    # CONSOLE SUMMARY
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("WALK-FORWARD DIAGNOSTIC REPORT")
    print("=" * 100)

    # Macro IS→OOS table
    print("\n╔══════════════════════════════════════════════════════════════════════╗")
    print("║                    宏观环境变化 (IS → OOS)                          ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    is_mrpt = df_regime[df_regime['Window'] == 'MRPT_IS'].iloc[0] if len(df_regime[df_regime['Window'] == 'MRPT_IS']) > 0 else None
    oos_mrpt = df_regime[df_regime['Window'].str.startswith('MRPT_OOS')]

    table_items = [
        ('VIX', 'VIX_mean', '{:.1f}'),
        ('SPY回报', 'SPY_return', '{:+.1%}'),
        ('HY利差', 'HY_Spread_mean', '{:.2f}%'),
        ('IG利差', 'IG_Spread_mean', '{:.2f}%'),
        ('收益率曲线(10Y-2Y)', 'Yield_Curve_10Y2Y_mean', '{:+.2f}%'),
        ('联储基金利率', 'Fed_Funds_EFFR_mean', '{:.2f}%'),
        ('通胀预期(10Y BE)', 'Breakeven_10Y_mean', '{:.2f}%'),
        ('金融压力(StL)', 'Financial_Stress_StL_mean', '{:.3f}'),
        ('NFCI', 'NFCI_mean', '{:.3f}'),
        ('消费者信心', 'Consumer_Sentiment_mean', '{:.1f}'),
        ('NVDA 回报', 'NVDA_return', '{:+.1%}'),
        ('SOXX 回报', 'SOXX_return', '{:+.1%}'),
        ('GLD 回报', 'GLD_return', '{:+.1%}'),
        ('USO 回报', 'USO_return', '{:+.1%}'),
        ('UUP 回报', 'UUP_return', '{:+.1%}'),
        ('失业率', 'Unemployment_Rate_mean', '{:.1f}%'),
        ('非农就业变化(千人)', 'Nonfarm_Payrolls_change', '{:+.0f}k'),
        ('初领失业金(万)', 'Initial_Claims_mean', '{:.1f}万'),
        ('续领失业金(万)', 'Continued_Claims_mean', '{:.1f}万'),
    ]

    # Columns that need scaling (raw FRED units → display units)
    scale_map = {'Initial_Claims_mean': 1e4, 'Continued_Claims_mean': 1e4}

    print(f"  {'指标':<22s}  {'IS期间':>12s}  {'OOS范围':>20s}  {'变化'}")
    print(f"  {'─'*22}  {'─'*12}  {'─'*20}  {'─'*40}")
    for name, col, fmt in table_items:
        is_val = is_mrpt[col] if is_mrpt is not None and col in is_mrpt.index and pd.notna(is_mrpt[col]) else np.nan
        oos_vals = oos_mrpt[col].dropna().values if col in oos_mrpt.columns else np.array([])
        if np.isnan(is_val) or len(oos_vals) == 0:
            print(f"  {name:<22s}  {'N/A':>12s}  {'N/A':>20s}")
            continue
        lo, hi = np.min(oos_vals), np.max(oos_vals)
        # Apply scaling for claims data
        divisor = scale_map.get(col, 1)
        is_str = fmt.format(is_val / divisor)
        oos_str = f"{fmt.format(lo / divisor)} ~ {fmt.format(hi / divisor)}"
        # Determine change description
        change = ''
        if 'return' in col.lower() or col == 'SPY_return':
            if is_val > 0.15 and lo < 0:
                change = '牛市→震荡'
            elif is_val > 0 and hi < 0:
                change = '正→负'
        elif 'VIX' in col:
            if hi - lo > 5:
                change = '波动加大'
        elif 'Yield' in col:
            if hi > is_val + 0.3:
                change = '曲线陡峭化'
        elif 'EFFR' in col:
            if hi < is_val:
                change = '降息周期'
        elif 'Unemployment' in col:
            if hi > is_val + 0.3:
                change = '就业恶化'
        elif 'Nonfarm' in col:
            if lo < 0:
                change = '就业疲弱'
        elif 'Initial_Claims' in col:
            if hi > is_val * 1.1:
                change = '上升'
        elif 'Continued_Claims' in col:
            if hi > is_val * 1.05:
                change = '偏高'
        print(f"  {name:<22s}  {is_str:>12s}  {oos_str:>20s}  {change}")

    print("╚══════════════════════════════════════════════════════════════════════╝")

    # OOS window performance
    print("\n── MRPT OOS Window Performance ──")
    for w in MRPT_WINDOWS:
        if w['type'] == 'OOS':
            wrow = df_regime[df_regime['Window'] == w['label']]
            vix = wrow['VIX_mean'].values[0] if len(wrow) > 0 and 'VIX_mean' in wrow.columns else np.nan
            spy = wrow['SPY_return'].values[0] if len(wrow) > 0 and 'SPY_return' in wrow.columns else np.nan
            s = w.get('oos_sharpe', np.nan)
            p = w.get('oos_pnl', np.nan)
            status = '✓' if pd.notna(p) and p > 0 else '✗'
            print(f"  {w['label']:20s}  {w['start']}→{w['end']}  Sharpe={s:+6.2f}  PnL=${p:+8,.0f}  VIX={vix:5.1f}  SPY={spy:+.1%}  {status}" if not np.isnan(vix) else f"  {w['label']:20s}  {w['start']}→{w['end']}  Sharpe={s:+6.2f}  PnL=${p:+8,.0f}")

    print("\n── MTFS OOS Window Performance ──")
    for w in MTFS_WINDOWS:
        if w['type'] == 'OOS':
            wrow = df_regime[df_regime['Window'] == w['label']]
            vix = wrow['VIX_mean'].values[0] if len(wrow) > 0 and 'VIX_mean' in wrow.columns else np.nan
            spy = wrow['SPY_return'].values[0] if len(wrow) > 0 and 'SPY_return' in wrow.columns else np.nan
            s = w.get('oos_sharpe', np.nan)
            p = w.get('oos_pnl', np.nan)
            status = '✓' if pd.notna(p) and p > 0 else '✗'
            print(f"  {w['label']:20s}  {w['start']}→{w['end']}  Sharpe={s:+6.2f}  PnL=${p:+8,.0f}  VIX={vix:5.1f}  SPY={spy:+.1%}  {status}" if not np.isnan(vix) else f"  {w['label']:20s}  {w['start']}→{w['end']}  Sharpe={s:+6.2f}  PnL=${p:+8,.0f}")

    # Cointegration summary
    print("\n── 协整检验 IS→OOS ──")
    for _, row in df_coint.iterrows():
        is_p = row.get('IS_Coint_pval', np.nan)
        oos_p = row.get('Avg_OOS_Coint_pval', np.nan)
        det = row.get('Coint_Deterioration', '')
        marker = ' ← 协整丧失!' if det == 'YES' else (' ← 边际恶化' if det == 'MARGINAL' else '')
        print(f"  {row['Strategy']:5s}  {row['Pair']:12s}  IS p={is_p:.3f}  OOS_avg p={oos_p:.3f}  {marker}")

    # Ticker overlap
    if not df_overlap.empty:
        print("\n── Ticker集中风险 ──")
        for _, row in df_overlap.iterrows():
            print(f"  {row['Risk']:6s}  {row['Ticker']:5s}  出现在{row['N_Pairs']}个{row['Strategy']} pair: {row['Pairs']}")

    # Cross-correlation summary
    print("\n── Cross-Correlation变化最大的ticker对 (IS→OOS) ──")
    shift_vals = []
    for i, t1 in enumerate(common_tickers):
        for j, t2 in enumerate(common_tickers):
            if i < j:
                v = corr_shift.loc[t1, t2]
                if not np.isnan(v):
                    shift_vals.append((t1, t2, corr_is.loc[t1, t2], corr_oos.loc[t1, t2], v))
    shift_vals.sort(key=lambda x: abs(x[4]), reverse=True)
    for t1, t2, is_c, oos_c, delta in shift_vals[:15]:
        direction = '↓' if delta < 0 else '↑'
        print(f"  {t1:5s}/{t2:5s}  IS={is_c:+.3f} → OOS={oos_c:+.3f}  Δ={delta:+.3f} {direction}")

    # Problem pairs
    print(f"\n── 问题配对统计 ──")
    problems = df_summary[df_summary['Issues'] != 'Stable']
    for _, row in problems.iterrows():
        print(f"  {row['Strategy']:5s}  {row['Pair']:12s}  {row['Sector']:12s}  {row['Issues']}")

    n_total = len(df_summary)
    n_stable = len(df_summary[df_summary['Issues'] == 'Stable'])
    print(f"\n  问题配对: {len(problems)}/{n_total}  |  稳定配对: {n_stable}/{n_total}")

    # Final conclusions
    print("\n" + "=" * 100)
    print("综合结论")
    print("=" * 100)
    print("""
  1. 过拟合 (根本原因): IS期间(2024.02-2025.07)是连续大牛市(SPY+29~33%)，所有pair在trending市场
     中的correlation被系统性抬高。IS训练的参数在OOS震荡市(SPY±5%)完全失效。

  2. Regime结构性变化: 收益率曲线从IS近倒挂(+0.08%)→OOS正常陡峭(+0.53~0.69%)；VIX从IS均值
     17.6→OOS在15-22间大幅波动；联储从紧缩→降息周期。利率/波动率体制发生了根本性转换。

  3. Correlation崩塌: MTFS几乎所有pair出现correlation崩塌(BK/ALL 0.39→0.04, ETR/AVB 0.38→-0.04)。
     IS学到的对冲比率和信号权重在OOS完全不适用。MRPT的pair也有类似但程度较轻的问题。

  4. 单腿价格跳跃+Ticker集中: CL(Colgate)在W5暴涨+22%导致3个MRPT pair连锁亏损；WST在W3/W5
     深跌-16~18%影响3个pair。Ticker重叠将单一事件放大为系统性损失。

  5. MTFS策略缺陷: MTFS对market regime变化极度敏感——W1暴亏-$28k(动量方向反转)，W6暴亏-$21k
     (美股大跌触发系统性动量反转)。MTFS在非趋势市场中基本无法盈利。

  6. 利息成本侵蚀: MRPT Gross +$16.5k被利息-$9.9k吃掉60%。$500k本金年化利息约2.7%，
     在Sharpe<0.5时几乎无法覆盖。
""")

    # ── Daily Report Macro Snapshot ──────────────────────────────────────────
    daily_report_macro = _read_daily_report_macro()
    if daily_report_macro:
        print("\n" + "=" * 100)
        print("当前宏观环境快照 (来自 daily_report)")
        print("=" * 100)
        print(daily_report_macro)

    return out_path


if __name__ == '__main__':
    main()
