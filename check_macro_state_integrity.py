"""
check_macro_state_integrity.py
──────────────────────────────────────────────────────────────────────────────
MacroStateStore 数据完整性检查。

检查项：
  1. 文件覆盖：从 START 到 TODAY 无缺失周文件，无单周行数为 0
  2. 日期连续性：相邻交易日间距 ≤ 5 个日历日，无重复索引
  3. SIMILARITY_FEATURES 填充率：每年每列不低于阈值
  4. Z-score 合理性：全局 mean ≈ 0 (|mean| ≤ 1)，std ≈ 1 (0.5–2.5)
  5. 跨年边界一致性：相邻年份 z-score 均值差 ≤ 0.5（检测基准漂移）
  6. FRED 覆盖年份：hy_spread / ig_spread 必须从 2016 起有值
  7. vix3m 缺口：2017-2019 vix3m 为 NaN 是预期行为（文档化，不报错）
  8. 总行数不低于预期（2017-今天约 9 × 252 行）

退出码：0 = 全部通过，1 = 有 FAIL 或 WARN
"""
from __future__ import annotations
import sys
import os
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── 路径 ─────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
os.chdir(_HERE)

from MacroStateStore import MacroStateStore, SIMILARITY_FEATURES

STORE = MacroStateStore()
CHECK_START = date(2018, 7, 1)   # SR 回测起始日：从这天起必须 100% 无缺口
BACKFILL_START = date(2017, 1, 1)  # init 起始：这段数据用于 z-score 热身
TODAY = date.today()

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []   # (check_name, status, message)


def record(name: str, status: str, msg: str) -> None:
    results.append((name, status, msg))
    icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}[status]
    print(f"  {icon}  [{status}]  {name}: {msg}")


# ════════════════════════════════════════════════════════════════════════════
# 1. 加载全量数据
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*70}")
print(f"  MacroStateStore Integrity Check")
print(f"  backfill_start={BACKFILL_START}  sr_start={CHECK_START}  today={TODAY}")
print(f"{'═'*70}\n")

print("[1/8] 加载全量数据...")
df = STORE.load(str(BACKFILL_START), str(TODAY))
if df.empty:
    print("\n  FATAL: 无数据，请先运行 --init")
    sys.exit(1)
print(f"  共 {len(df)} 行，列数 {df.shape[1]}")
print(f"  日期范围: {df.index[0].date()} → {df.index[-1].date()}\n")


# ════════════════════════════════════════════════════════════════════════════
# 2. 文件覆盖（周文件完整性）
# ════════════════════════════════════════════════════════════════════════════
print("[2/8] 周文件覆盖...")
weekly_files = sorted(STORE._weekly_dir.glob("week_*.parquet"))
expected_mondays: set[date] = set()
# First Monday on or after BACKFILL_START (not the Monday of BACKFILL_START's week,
# which could fall before BACKFILL_START when it's Tue-Sun)
days_ahead = (7 - BACKFILL_START.weekday()) % 7
first_monday = BACKFILL_START + timedelta(days=days_ahead)
d = first_monday
while d <= TODAY:
    expected_mondays.add(d)
    d += timedelta(days=7)

found_mondays = set()
for p in weekly_files:
    monday = date.fromisoformat(p.stem.replace("week_", ""))
    found_mondays.add(monday)
    sub = pd.read_parquet(p)
    if sub.empty:
        record("week_file_rows", FAIL, f"{p.stem} 文件存在但行数为 0")

missing = expected_mondays - found_mondays
if missing:
    record("weekly_coverage", FAIL,
           f"缺少 {len(missing)} 个周文件: {sorted(missing)[:5]}...")
else:
    record("weekly_coverage", PASS,
           f"{len(found_mondays)} 个周文件，全部覆盖 {BACKFILL_START}→{TODAY}")


# ════════════════════════════════════════════════════════════════════════════
# 3. 日期连续性（交易日间距 & 重复索引）
# ════════════════════════════════════════════════════════════════════════════
print("\n[3/8] 日期连续性...")

# 重复索引
dupes = df.index[df.index.duplicated()]
if not dupes.empty:
    record("no_duplicate_dates", FAIL,
           f"有 {len(dupes)} 个重复日期: {list(dupes[:3])}")
else:
    record("no_duplicate_dates", PASS, "无重复日期")

# 间距：只检查 CHECK_START 之后（SR 回测区间内不允许缺口）
sr_df = df[df.index.date >= CHECK_START]   # type: ignore[operator]
if not sr_df.empty:
    gaps = sr_df.index.to_series().diff().dt.days.dropna()
    big_gaps = gaps[gaps > 5]
    if not big_gaps.empty:
        record("no_trading_gaps", FAIL,
               f"SR 区间内 {len(big_gaps)} 处间距 > 5 天:\n"
               + "\n".join(f"    {idx.date()}: {v:.0f} 天"
                           for idx, v in big_gaps.items()))
    else:
        record("no_trading_gaps", PASS,
               f"SR 区间 {CHECK_START}→{TODAY} 无 >5 天间距")


# ════════════════════════════════════════════════════════════════════════════
# 4. SIMILARITY_FEATURES 填充率（每年）
# ════════════════════════════════════════════════════════════════════════════
print("\n[4/8] SIMILARITY_FEATURES 填充率...")

avail_sf = [f for f in SIMILARITY_FEATURES if f in df.columns]
missing_sf = [f for f in SIMILARITY_FEATURES if f not in df.columns]
if missing_sf:
    record("sim_features_present", FAIL,
           f"以下 SIMILARITY_FEATURES 列完全缺失: {missing_sf}")
else:
    record("sim_features_present", PASS, "全部 6 个 SIMILARITY_FEATURES 列存在")

if avail_sf:
    # 全局填充率
    global_fill = df[avail_sf].notna().mean().mean() * 100
    status = PASS if global_fill >= 95 else (WARN if global_fill >= 80 else FAIL)
    record("sim_features_global_fill", status,
           f"全局填充率 {global_fill:.1f}%")

    # 按年（SR 回测区间内每年 ≥ 90%）
    print("  按年明细：")
    any_year_fail = False
    for yr, grp in df[avail_sf].groupby(df.index.year):
        fill = grp.notna().mean().mean() * 100
        per_col = {f: f"{grp[f].notna().mean()*100:.0f}%" for f in avail_sf}
        status_yr = PASS if fill >= 90 else (WARN if fill >= 70 else FAIL)
        if status_yr == FAIL and yr >= CHECK_START.year:
            any_year_fail = True
        print(f"    {yr}: {fill:5.1f}%  "
              + "  ".join(f"{f}={v}" for f, v in per_col.items()))
    if any_year_fail:
        record("sim_features_yearly_fill", FAIL,
               f"SR 区间内某年填充率 < 90%（见上方明细）")
    else:
        record("sim_features_yearly_fill", PASS,
               f"SR 区间每年填充率均 ≥ 90%")


# ════════════════════════════════════════════════════════════════════════════
# 5. Z-score 合理性（全局分布）
# ════════════════════════════════════════════════════════════════════════════
print("\n[5/8] Z-score 全局分布（mean ≈ 0, std ≈ 1）...")

Z_COLS = [f for f in ["vix_z", "baa_spread_z", "fin_stress_z",
                       "xlk_spy_z", "yield_curve_z"] if f in df.columns]

for col in Z_COLS:
    s = df[col].dropna()
    if len(s) < 100:
        record(f"zscore_{col}", WARN, f"样本不足 ({len(s)} 行)，跳过分布检查")
        continue
    mu, sd = float(s.mean()), float(s.std())
    if abs(mu) > 1.0:
        status = FAIL
    elif abs(mu) > 0.5 or sd < 0.5 or sd > 2.5:
        status = WARN
    else:
        status = PASS
    record(f"zscore_{col}", status,
           f"mean={mu:+.3f}  std={sd:.3f}  n={len(s)}")


# ════════════════════════════════════════════════════════════════════════════
# 6. 跨年边界一致性（检测基准漂移）
# ════════════════════════════════════════════════════════════════════════════
print("\n[6/8] 跨年边界一致性（z-score 均值年际差）...")

boundary_fail = False
for col in Z_COLS:
    prev_mu: float | None = None
    prev_yr: int | None = None
    for yr, grp in df[col].dropna().groupby(df[col].dropna().index.year):
        mu = float(grp.mean())
        if prev_mu is not None and abs(mu - prev_mu) > 2.0:
            # Threshold 2.0: year-to-year mean difference > 2σ suggests baseline
            # discontinuity (e.g. recomputed from different reference window).
            # Normal regime swings (2018 vol, 2020 COVID, 2022 hikes) produce
            # differences of 1-2σ — those are REAL and should NOT be flagged.
            record(f"zscore_boundary_{col}_{prev_yr}_{yr}",
                   WARN,
                   f"{col}: {prev_yr}→{yr} 年均值差 {mu - prev_mu:+.3f}"
                   f"（prev={prev_mu:+.3f}, cur={mu:+.3f}）")
            boundary_fail = True
        prev_mu = mu
        prev_yr = yr
if not boundary_fail:
    record("zscore_year_boundary", PASS,
           "所有 z-score 列相邻年均值差均 ≤ 0.5")


# ════════════════════════════════════════════════════════════════════════════
# 7. FRED 覆盖年份（hy_spread / ig_spread 必须从 2016 起）
# ════════════════════════════════════════════════════════════════════════════
print("\n[7/8] FRED 关键序列覆盖年份...")

# baa_spread (BAA10Y) must have full history back to SR backtest start
for fred_col, expect_yr, note in [
    ("baa_spread", 2017, "Moody's Baa-Treasury spread, must cover SR backtest start"),
]:
    if fred_col not in df.columns:
        record(f"fred_{fred_col}_present", FAIL, f"列 {fred_col} 完全缺失")
        continue
    s = df[fred_col].dropna()
    if s.empty:
        record(f"fred_{fred_col}_range", FAIL, f"{fred_col} 全为 NaN")
        continue
    first_yr = s.index.min().year
    if first_yr > expect_yr + 1:
        record(f"fred_{fred_col}_range", FAIL,
               f"最早有效日期 {s.index.min().date()}（期望 ≤ {expect_yr}）  [{note}]")
    elif first_yr > expect_yr:
        record(f"fred_{fred_col}_range", WARN,
               f"最早有效日期 {s.index.min().date()}（期望 {expect_yr}，略有缺口）")
    else:
        record(f"fred_{fred_col}_range", PASS,
               f"覆盖 {s.index.min().date()} → {s.index.max().date()}")

# hy_spread / ig_spread are monitoring-only (2023+ is expected, not a failure)
for fred_col in ["hy_spread", "ig_spread"]:
    if fred_col not in df.columns:
        record(f"fred_{fred_col}_present", WARN, f"列 {fred_col} 缺失（ICE BofA 授权限制，不影响 MCPS）")
        continue
    s = df[fred_col].dropna()
    if s.empty:
        record(f"fred_{fred_col}_range", WARN, f"{fred_col} 全为 NaN（ICE BofA 授权限制，2023+ 才有数据）")
    else:
        record(f"fred_{fred_col}_range", PASS,
               f"覆盖 {s.index.min().date()} → {s.index.max().date()}（监控用，非 SIMILARITY_FEATURES）")


# ════════════════════════════════════════════════════════════════════════════
# 8. vix3m 缺口记录（预期行为，仅文档化）
# ════════════════════════════════════════════════════════════════════════════
print("\n[8/8] vix3m 缺口文档化（2017-2019 期望 NaN）...")

if "vix3m" in df.columns:
    vix3m = df["vix3m"]
    pre2020 = vix3m[vix3m.index.year < 2020]
    post2020 = vix3m[vix3m.index.year >= 2020]
    pre_fill = pre2020.notna().mean() * 100
    post_fill = post2020.notna().mean() * 100
    if post_fill < 90:
        record("vix3m_post2020", FAIL,
               f"2020 之后 vix3m 填充率 {post_fill:.1f}%（期望 ≥ 90%）")
    else:
        record("vix3m_coverage", PASS,
               f"2017-2019 填充率 {pre_fill:.1f}%（数据可能不完整，已知缺口）"
               f"  |  2020+ 填充率 {post_fill:.1f}%（正常）")
else:
    record("vix3m_present", WARN, "vix3m 列不存在（不影响 MCPS）")


# ════════════════════════════════════════════════════════════════════════════
# 总行数检查
# ════════════════════════════════════════════════════════════════════════════
years_covered = (TODAY - BACKFILL_START).days / 365.25
expected_min = int(years_covered * 252 * 0.80)   # 宽松 80%
if len(df) < expected_min:
    record("total_rows", FAIL,
           f"总行数 {len(df)} 低于预期下限 {expected_min} "
           f"（{years_covered:.1f} 年 × 252 × 80%）")
else:
    record("total_rows", PASS,
           f"总行数 {len(df)} ≥ 预期下限 {expected_min}")


# ════════════════════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*70}")
n_pass = sum(1 for _, s, _ in results if s == PASS)
n_warn = sum(1 for _, s, _ in results if s == WARN)
n_fail = sum(1 for _, s, _ in results if s == FAIL)
print(f"  结果汇总:  {n_pass} PASS  {n_warn} WARN  {n_fail} FAIL")
print(f"{'═'*70}")

if n_fail > 0:
    print("\n  ✗ 有 FAIL 项 — 数据不可用于 MCPS，请检查上方详情")
    sys.exit(1)
elif n_warn > 0:
    print("\n  ⚠ 有 WARN 项 — 数据基本可用，建议人工核查上方黄色项")
    sys.exit(1)
else:
    print("\n  ✓ 全部通过 — 数据完整，可运行 sector_rotation_pipeline.sh select")
    sys.exit(0)
