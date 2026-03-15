#!/usr/bin/env python3
"""
通用 Portfolio Reconciliation 检查脚本
支持任意 pair/symbol 配置，自动从 Excel 推断所有 symbol 和 pair
"""

import sys
import os
import glob
import pandas as pd
import numpy as np
import math
import warnings
warnings.filterwarnings('ignore')

if len(sys.argv) > 1:
    EXCEL_PATH = sys.argv[1]
else:
    # Auto-pick latest portfolio_history_*.xlsx in historical_runs/
    _base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'historical_runs')
    _files = sorted(glob.glob(os.path.join(_base, 'portfolio_history_*.xlsx')), key=os.path.getmtime)
    if not _files:
        raise FileNotFoundError("No portfolio_history_*.xlsx found in historical_runs/")
    EXCEL_PATH = _files[-1]

xl = pd.ExcelFile(EXCEL_PATH)
sheets = {sheet: xl.parse(sheet) for sheet in xl.sheet_names}
print(f"已加载 Excel: {EXCEL_PATH}")
print(f"Sheet 列表: {xl.sheet_names}\n")

# ── 自动推断所有 pair 和 symbol ──────────────────────────────────────────────
trades_raw = sheets['pair_trade_history'].copy()
trades_raw['Date'] = pd.to_datetime(trades_raw['Date'])
all_pairs = sorted(trades_raw['Pair'].dropna().unique())
all_symbols = sorted(trades_raw['Symbol'].dropna().unique())
print(f"检测到 pair: {all_pairs}")
print(f"检测到 symbol: {all_symbols}\n")

PASS = 0
FAIL = 0

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")
    if detail:
        print(f"    {detail}")

# ============================================================
# CHECK 1: Invariant A — share_history_by_pair 加总 == share_history (symbol 级净持仓)
# ============================================================
print("=" * 70)
print("CHECK 1: Invariant A — share_history_by_pair 各 pair 加总 == share_history")
print("=" * 70)

sh_bp = sheets['share_history_by_pair'].copy()  # cols: Date, Pair, Symbol, Value
sh_bp['Date'] = pd.to_datetime(sh_bp['Date'])

sh_sym = sheets['share_history'].copy()  # wide format: Date, SYM1, SYM2, ...
sh_sym['Date'] = pd.to_datetime(sh_sym['Date'])
sym_cols = [c for c in sh_sym.columns if c != 'Date']
sh_sym_long = sh_sym.melt(id_vars=['Date'], value_vars=sym_cols, var_name='Symbol', value_name='shares_sym')

# by_pair 加总 (col is 'Value')
sh_bp_sum = sh_bp.groupby(['Date', 'Symbol'])['Value'].sum().reset_index()
sh_bp_sum.columns = ['Date', 'Symbol', 'shares_pair_sum']

merged_a = sh_bp_sum.merge(sh_sym_long, on=['Date', 'Symbol'], how='inner')
merged_a['diff'] = (merged_a['shares_pair_sum'] - merged_a['shares_sym']).abs()
fail_a = merged_a[merged_a['diff'] > 0.5]
check("Invariant A", len(fail_a) == 0,
      f"{len(fail_a)} failures / {len(merged_a)} checks" if len(fail_a) > 0 else f"{len(merged_a)} checks passed")
if len(fail_a) > 0:
    print(fail_a.head(5).to_string())

# ============================================================
# CHECK 2: Invariant C — acc_sec_pnl_by_pair 各 pair 加总 == acc_security_pnl_history
# ============================================================
print("\n" + "=" * 70)
print("CHECK 2: Invariant C — acc_sec_pnl_by_pair 跨 pair 加总 == acc_security_pnl_history")
print("=" * 70)

sbp = sheets['acc_sec_pnl_by_pair'].copy()
sbp['Date'] = pd.to_datetime(sbp['Date'])
sbp_sum = sbp.groupby(['Date', 'Symbol'])['PnL Dollar'].sum().reset_index()
sbp_sum.columns = ['Date', 'Symbol', 'pair_sum']

sec_pnl = sheets['acc_security_pnl_history'].copy()
sec_pnl['Date'] = pd.to_datetime(sec_pnl['Date'])

merged_c = sbp_sum.merge(sec_pnl[['Date', 'Symbol', 'PnL Dollar']], on=['Date', 'Symbol'], how='inner')
merged_c['diff'] = (merged_c['pair_sum'] - merged_c['PnL Dollar']).abs()
fail_c = merged_c[merged_c['diff'] > 1e-4]
check("Invariant C", len(fail_c) == 0,
      f"{len(fail_c)} failures / {len(merged_c)} checks" if len(fail_c) > 0 else f"{len(merged_c)} checks passed")
if len(fail_c) > 0:
    print(fail_c.head(5).to_string())

# ============================================================
# CHECK 3: Invariant D — acc_sec_pnl_by_pair pair 内两腿加总 == acc_pair_trade_pnl_history
# ============================================================
print("\n" + "=" * 70)
print("CHECK 3: Invariant D — pair 内两腿 PnL 加总 == acc_pair_trade_pnl_history")
print("=" * 70)

sbp_pair_sum = sbp.groupby(['Date', 'Pair'])['PnL Dollar'].sum().reset_index()
sbp_pair_sum.columns = ['Date', 'Pair', 'sym_sum']

acc_pair = sheets['acc_pair_trade_pnl_history'].copy()
acc_pair['Date'] = pd.to_datetime(acc_pair['Date'])

merged_d = sbp_pair_sum.merge(acc_pair[['Date', 'Pair', 'PnL Dollar']], on=['Date', 'Pair'], how='inner')
merged_d['diff'] = (merged_d['sym_sum'] - merged_d['PnL Dollar']).abs()
fail_d = merged_d[merged_d['diff'] > 1e-4]
check("Invariant D", len(fail_d) == 0,
      f"{len(fail_d)} failures / {len(merged_d)} checks" if len(fail_d) > 0 else f"{len(merged_d)} checks passed")
if len(fail_d) > 0:
    print(fail_d.head(5).to_string())

# ============================================================
# CHECK 4: finished_trades_pnl_by_pair 各 pair 加总 == finished_trades_pnl
# ============================================================
print("\n" + "=" * 70)
print("CHECK 4: finished_trades_pnl_by_pair 加总 == finished_trades_pnl")
print("=" * 70)

fin_bp = sheets['finished_trades_pnl_by_pair'].copy()
fin_sym = sheets['finished_trades_pnl'].copy()

fin_bp_sum = fin_bp.groupby('Symbol')['Value'].sum().reset_index()
fin_bp_sum.columns = ['Symbol', 'pair_sum']
merged_fin = fin_bp_sum.merge(fin_sym[['Symbol', 'Value']], on='Symbol', how='outer').fillna(0)
merged_fin['diff'] = (merged_fin['pair_sum'] - merged_fin['Value']).abs()
fail_fin = merged_fin[merged_fin['diff'] > 1e-4]
check("finished_trades_pnl_by_pair 加总", len(fail_fin) == 0,
      f"{len(fail_fin)} mismatches" if len(fail_fin) > 0 else f"{len(merged_fin)} symbols passed")
if len(fail_fin) > 0:
    print(merged_fin.to_string())

# ============================================================
# CHECK 5: total_cost_history_by_pair 加总 == total_cost_history
# ============================================================
print("\n" + "=" * 70)
print("CHECK 5: total_cost_history_by_pair 加总 == total_cost_history")
print("=" * 70)

tc_bp = sheets['total_cost_history_by_pair'].copy()
tc_sym = sheets['total_cost_history'].copy()

tc_bp_sum = tc_bp.groupby('Symbol')['Value'].sum().reset_index()
tc_bp_sum.columns = ['Symbol', 'pair_sum']
merged_tc = tc_bp_sum.merge(tc_sym[['Symbol', 'Value']], on='Symbol', how='outer').fillna(0)
merged_tc['diff'] = (merged_tc['pair_sum'] - merged_tc['Value']).abs()
fail_tc = merged_tc[merged_tc['diff'] > 1e-4]
check("total_cost_history_by_pair 加总", len(fail_tc) == 0,
      f"{len(fail_tc)} mismatches" if len(fail_tc) > 0 else f"{len(merged_tc)} symbols passed")
if len(fail_tc) > 0:
    print(merged_tc.to_string())

# ============================================================
# CHECK 6: 无持仓时 acc_security_pnl 应等于已实现 PnL（不应有 MTM 残留）
# ============================================================
print("\n" + "=" * 70)
print("CHECK 6: 无持仓时 acc_security_pnl 不应有 MTM 残留（只有已实现部分）")
print("=" * 70)

# 构建每个 (date, symbol) 的净持仓
sh_sym_dict = {}
for _, row in sh_sym_long.iterrows():
    sh_sym_dict[(row['Date'], row['Symbol'])] = row['shares_sym']

# 无持仓 = 所有含该 symbol 的 pair 的 by_pair 持仓都为 0
bp_net = sh_bp.groupby(['Date', 'Symbol'])['Value'].sum().reset_index()
bp_net.columns = ['Date', 'Symbol', 'Shares']
no_pos = bp_net[bp_net['Shares'] == 0].copy()

# 获取平仓日（有 finished_pnl 变化，允许 PnL != 0）
close_dates_set = set(trades_raw[trades_raw['Order Type'] == 'close']['Date'].dt.date)

# Check 6 设计意图：空仓期间 acc_security_pnl_by_pair[pair][symbol].pnl_dollar
# 应保持冻结（不增加），即相邻两天的差值为 0
# 用 acc_sec_pnl_by_pair（sbp）检查：空仓时相邻日 pnl_dollar 变化应为 0
sh_bp_idx = sh_bp.set_index(['Date', 'Pair', 'Symbol'])['Value']
sbp6 = sbp.sort_values(['Pair', 'Symbol', 'Date']).copy()
sbp6['Shares'] = sbp6.apply(lambda r: sh_bp_idx.get((r['Date'], r['Pair'], r['Symbol']), 0), axis=1)
sbp6['prev_pnl'] = sbp6.groupby(['Pair', 'Symbol'])['PnL Dollar'].shift(1)
sbp6['delta'] = (sbp6['PnL Dollar'] - sbp6['prev_pnl']).abs()
sbp6['is_close_day'] = sbp6['Date'].dt.date.isin(close_dates_set)
# 空仓 + 非平仓日 + pnl 有变化 → MTM 残留
anomaly6 = sbp6[
    (sbp6['Shares'] == 0) &
    (~sbp6['is_close_day']) &
    (sbp6['delta'] > 1e-4) &
    (sbp6['prev_pnl'].notna())
]
check("无持仓非平仓日 PnL 无变化（无 MTM 残留）", len(anomaly6) == 0,
      f"{len(anomaly6)} anomalies" if len(anomaly6) > 0 else
      f"{len(sbp6[(sbp6['Shares']==0) & (sbp6['prev_pnl'].notna())])} checks passed")
if len(anomaly6) > 0:
    print(anomaly6[['Date', 'Symbol', 'Shares', 'PnL Dollar']].head(10).to_string())

# ============================================================
# CHECK 7: PnL Dollar != 0 时 PnL Percent 不应为 0（acc_sec_pnl_by_pair）
# ============================================================
print("\n" + "=" * 70)
print("CHECK 7: acc_sec_pnl_by_pair — PnL Dollar!=0 时 PnL Percent 不为 0")
print("=" * 70)

bad7 = sbp[(sbp['PnL Dollar'].abs() > 1e-4) & (sbp['PnL Percent'].abs() < 1e-10)]
total_nonzero = sbp[sbp['PnL Dollar'].abs() > 1e-4]
check("acc_sec_pnl_by_pair PnL Percent 不为零", len(bad7) == 0,
      f"{len(bad7)}/{len(total_nonzero)} 行 PnL Dollar!=0 但 PnL Percent=0" if len(bad7) > 0
      else f"{len(total_nonzero)} 行全部通过")
if len(bad7) > 0:
    print(bad7.head(10).to_string())

# ============================================================
# CHECK 8: PnL Dollar != 0 时 PnL Percent 不应为 0（acc_pair_trade_pnl_history）
# ============================================================
print("\n" + "=" * 70)
print("CHECK 8: acc_pair_trade_pnl_history — PnL Dollar!=0 时 PnL Percent 不为 0")
print("=" * 70)

bad8 = acc_pair[(acc_pair['PnL Dollar'].abs() > 1e-4) & (acc_pair['PnL Percent'].abs() < 1e-10)]
total8 = acc_pair[acc_pair['PnL Dollar'].abs() > 1e-4]
check("acc_pair_trade_pnl_history PnL Percent 不为零", len(bad8) == 0,
      f"{len(bad8)}/{len(total8)} 行 PnL Dollar!=0 但 PnL Percent=0" if len(bad8) > 0
      else f"{len(total8)} 行全部通过")
if len(bad8) > 0:
    print(bad8.head(10).to_string())

# ============================================================
# CHECK 9: dod_pair_trade_pnl_history — 同 pair 不同 symbol 不应共用同一值（共享 symbol bug）
# ============================================================
print("\n" + "=" * 70)
print("CHECK 9: dod_pair_trade_pnl_history 共享 symbol 不应重复（CL/AWK/WST 跨 pair 独立）")
print("=" * 70)

dod_pair = sheets['dod_pair_trade_pnl_history'].copy()
dod_pair['Date'] = pd.to_datetime(dod_pair['Date'])

# 找同一 date 下多个含相同 symbol 的 pair 是否有相同 PnL Dollar（非零）
# 例如 CL/WST, CL/SRE, CL/GD 同一天都有同一个非零值 → 重复
shared_symbols = {}
for pair in all_pairs:
    parts = pair.split('/')
    for s in parts:
        shared_symbols.setdefault(s, []).append(pair)
shared_symbols = {s: ps for s, ps in shared_symbols.items() if len(ps) > 1}

dup_count = 0
for sym, sym_pairs in shared_symbols.items():
    sub = dod_pair[dod_pair['Pair'].isin(sym_pairs)].copy()
    # 找同一 date 下所有含该 symbol 的 pair 的 PnL Dollar 完全相同（非零）
    for date, grp in sub.groupby('Date'):
        nonzero = grp[grp['PnL Dollar'].abs() > 1e-4]
        if len(nonzero) > 1:
            vals = nonzero['PnL Dollar'].values
            if np.all(np.abs(vals - vals[0]) < 1e-4):
                dup_count += 1
                if dup_count <= 3:
                    print(f"  潜在重复: {date.date()} symbol={sym} pairs={nonzero['Pair'].tolist()} PnL={vals[0]:.4f}")

check("dod_pair 共享 symbol 无重复值", dup_count == 0,
      f"发现 {dup_count} 个潜在重复日期" if dup_count > 0 else "无重复")

# ============================================================
# CHECK 10: 手工从 pair_trade_history 计算每个 pair 的已实现 PnL，对比 finished_trades_pnl_by_pair
# ============================================================
print("\n" + "=" * 70)
print("CHECK 10: 手工计算 per-pair 已实现 PnL vs finished_trades_pnl_by_pair")
print("=" * 70)

fin_bp_dict = {}
for _, row in fin_bp.iterrows():
    fin_bp_dict[(row['Pair'], row['Symbol'])] = row['Value']

fail10 = 0
total10 = 0
for pair in all_pairs:
    pair_trades = trades_raw[trades_raw['Pair'] == pair].sort_values('Date')
    for sym in pair.split('/'):
        sym_trades = pair_trades[pair_trades['Symbol'] == sym]
        if len(sym_trades) == 0:
            continue
        opens = []
        realized = 0.0
        for _, t in sym_trades.iterrows():
            amt = t['Amount']
            price = t['Price']
            otype = t['Order Type']
            direction = t.get('Direction', '')
            if otype == 'open':
                opens.append({'amount': amt, 'price': price, 'direction': direction})
            elif otype == 'close' and opens:
                opener = opens.pop(0)
                shares = abs(opener['amount'])
                if opener['direction'] == 'short':
                    pnl = (opener['price'] - price) * shares
                else:
                    pnl = (price - opener['price']) * shares
                realized += pnl
        total10 += 1
        stored = fin_bp_dict.get((pair, sym), 0)
        diff = abs(realized - stored)
        if diff > 1.0:
            fail10 += 1
            print(f"  ✗ {pair}/{sym}: 手工={realized:.4f} stored={stored:.4f} diff={diff:.4f}")

check("手工计算 per-pair 已实现 PnL", fail10 == 0,
      f"{fail10}/{total10} 不一致" if fail10 > 0 else f"{total10} pair/symbol 全部通过")

# ============================================================
# CHECK 11: cost_basis_by_pair 持仓期内不应为 0，平仓后应为 0
# ============================================================
print("\n" + "=" * 70)
print("CHECK 11: cost_basis_by_pair 持仓期间不为 0，空仓时为 0")
print("=" * 70)

cb_bp = sheets['cost_basis_by_pair'].copy()
cb_bp['Date'] = pd.to_datetime(cb_bp['Date'])

sh_bp_dict = {}
for _, row in sh_bp.iterrows():
    sh_bp_dict[(row['Date'], row['Pair'], row['Symbol'])] = row['Value']

fail11_pos = 0
fail11_zero = 0
cb_bp_merged = cb_bp.merge(sh_bp[['Date', 'Pair', 'Symbol', 'Value']].rename(columns={'Value': 'Shares'}), on=['Date', 'Pair', 'Symbol'], how='left')
cb_bp_merged['Shares'] = cb_bp_merged['Shares'].fillna(0)

# 持仓非零但 cb=0
bad11a = cb_bp_merged[(cb_bp_merged['Shares'].abs() > 0.5) & (cb_bp_merged['Value'].abs() < 1e-6)]
# 无持仓但 cb!=0
bad11b = cb_bp_merged[(cb_bp_merged['Shares'].abs() < 0.5) & (cb_bp_merged['Value'].abs() > 1e-6)]

check("持仓时 cost_basis 不为 0", len(bad11a) == 0,
      f"{len(bad11a)} 行持仓非零但 cb=0" if len(bad11a) > 0 else f"通过")
if len(bad11a) > 0:
    print(bad11a.head(5).to_string())

check("空仓时 cost_basis 为 0", len(bad11b) == 0,
      f"{len(bad11b)} 行空仓但 cb!=0" if len(bad11b) > 0 else f"通过")
if len(bad11b) > 0:
    print(bad11b.head(5).to_string())

# ============================================================
# CHECK 12: acc_security_pnl_history PnL Percent 一致性
# ============================================================
print("\n" + "=" * 70)
print("CHECK 12: acc_security_pnl_history — PnL Dollar!=0 时 PnL Percent 不为 0")
print("=" * 70)

bad12 = sec_pnl[(sec_pnl['PnL Dollar'].abs() > 1e-4) & (sec_pnl['PnL Percent'].abs() < 1e-10)]
total12 = sec_pnl[sec_pnl['PnL Dollar'].abs() > 1e-4]
check("acc_security_pnl_history PnL Percent 不为零", len(bad12) == 0,
      f"{len(bad12)}/{len(total12)} 行 PnL Dollar!=0 但 PnL Percent=0" if len(bad12) > 0
      else f"{len(total12)} 行全部通过")
if len(bad12) > 0:
    print(bad12.head(10).to_string())

# ============================================================
# CHECK 13: dod 一致性 — sum(dod) ≈ 最终 acc（对 acc_pair_trade_pnl_history）
# ============================================================
print("\n" + "=" * 70)
print("CHECK 13: sum(dod_pair_trade_pnl) ≈ 最终 acc_pair_trade_pnl（per pair）")
print("=" * 70)

dod_sum_by_pair = dod_pair.groupby('Pair')['PnL Dollar'].sum().reset_index()
dod_sum_by_pair.columns = ['Pair', 'dod_sum']

last_date_acc = acc_pair['Date'].max()
acc_last = acc_pair[acc_pair['Date'] == last_date_acc][['Pair', 'PnL Dollar']].copy()
acc_last.columns = ['Pair', 'acc_last']

merged13 = dod_sum_by_pair.merge(acc_last, on='Pair', how='outer').fillna(0)
merged13['diff'] = (merged13['dod_sum'] - merged13['acc_last']).abs()
fail13 = merged13[merged13['diff'] > 1.0]
check("sum(dod) ≈ acc 最终值 (per pair)", len(fail13) == 0,
      f"{len(fail13)} pairs 不一致" if len(fail13) > 0 else f"{len(merged13)} pairs 通过")
if len(fail13) > 0:
    print(merged13[merged13['diff'] > 1.0].to_string())

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 70)
print(f"汇总: {PASS} 项通过 / {FAIL} 项失败 / 共 {PASS + FAIL} 项")
print("=" * 70)
