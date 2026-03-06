import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

with open('price_data/earnings_cache.json') as f:
    cache = json.load(f)

earnings_by_sym = {}
for sym, entries in cache['symbols'].items():
    earnings_by_sym[sym] = set(e['earnings_date'] for e in entries)

BLACKOUT = 1

def in_blackout(sym, date_str):
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    for ed_str in earnings_by_sym.get(sym, set()):
        ed = datetime.strptime(ed_str, '%Y-%m-%d').date()
        if abs((d - ed).days) <= BLACKOUT:
            return True, ed_str
    return False, None

bt_path = 'historical_runs/portfolio_history_all15_best_per_pair_default_20260304_021640.xlsx'
pt  = pd.read_excel(bt_path, sheet_name='pair_trade_history')
acc = pd.read_excel(bt_path, sheet_name='acc_pair_trade_pnl_history')
sl  = pd.read_excel(bt_path, sheet_name='stop_loss_history')
pt['Date']  = pd.to_datetime(pt['Date'])
acc['Date'] = pd.to_datetime(acc['Date'])

PAIRS = [
    'MSCI/LII','D/MCHP','DG/MOS','ESS/EXPD','ACGL/UHS',
    'AAPL/META','YUM/MCD','GS/ALLY','CL/USO','ALGN/UAL',
    'ARES/CG','AMG/BEN','LYFT/UBER','TW/CME','CART/DASH',
]

hdr = "# | Pair            | Open Date  | BlkSym      | Earn Date  | PnL ($)    | Reason"
print(hdr)
print('-' * len(hdr))

blocked_pnl = []
allowed_pnl = []

for pair in PAIRS:
    s1, s2 = pair.split('/')
    p = pt[pt['Pair'] == pair].copy().sort_values('Date')
    opens  = p[(p['Order Type'] == 'open')  & (p['Symbol'] == s1)].reset_index(drop=True)
    closes = p[(p['Order Type'] == 'close') & (p['Symbol'] == s1)].reset_index(drop=True)
    a = acc[acc['Pair'] == pair].sort_values('Date').reset_index(drop=True)
    sl_pair = sl[sl['Pair'] == pair] if (not sl.empty and 'Pair' in sl.columns) else pd.DataFrame()

    n = min(len(opens), len(closes))
    for i in range(n):
        od  = opens.iloc[i]['Date']
        cd  = closes.iloc[i]['Date']
        ods = od.strftime('%Y-%m-%d')

        a_after  = a[a['Date'] <= cd]
        a_before = a[a['Date'] <  od]
        pnl = (a_after['PnL Dollar'].iloc[-1]  if not a_after.empty  else 0) - \
              (a_before['PnL Dollar'].iloc[-1] if not a_before.empty else 0)

        sl_dates = set(pd.to_datetime(sl_pair['Date']).dt.strftime('%Y-%m-%d')) if not sl_pair.empty else set()
        reason = 'Stop Loss' if cd.strftime('%Y-%m-%d') in sl_dates else 'Signal'

        blocked = False
        block_sym = ''
        earn_d = ''
        for sym in [s1, s2]:
            b, ed = in_blackout(sym, ods)
            if b:
                blocked = True
                block_sym = sym
                earn_d = ed
                break

        if blocked:
            blocked_pnl.append(pnl)
            sign = '+' if pnl >= 0 else ''
            print(f"{i+1:>2} | {pair:<15s} | {ods} | {block_sym:<11s} | {earn_d} | {sign}{pnl:>10,.0f} | {reason}")
        else:
            allowed_pnl.append(pnl)

print()
print("=== SUMMARY ===")
b_wins = sum(1 for x in blocked_pnl if x > 0)
a_wins = sum(1 for x in allowed_pnl if x > 0)
print(f"Blocked trades: {len(blocked_pnl)}")
print(f"  Total PnL:  {sum(blocked_pnl):>10,.0f}")
print(f"  Avg PnL:    {np.mean(blocked_pnl) if blocked_pnl else 0:>10,.0f}")
print(f"  Win Rate:   {100*b_wins/len(blocked_pnl) if blocked_pnl else 0:.0f}%  ({b_wins}W / {len(blocked_pnl)-b_wins}L)")
print()
print(f"Allowed trades: {len(allowed_pnl)}")
print(f"  Total PnL:  {sum(allowed_pnl):>10,.0f}")
print(f"  Avg PnL:    {np.mean(allowed_pnl) if allowed_pnl else 0:>10,.0f}")
print(f"  Win Rate:   {100*a_wins/len(allowed_pnl) if allowed_pnl else 0:.0f}%  ({a_wins}W / {len(allowed_pnl)-a_wins}L)")
print()
total = sum(blocked_pnl) + sum(allowed_pnl)
print(f"All trades total PnL:  {total:>10,.0f}")
print(f"After earnings filter: {sum(allowed_pnl):>10,.0f}  (blocked: {sum(blocked_pnl):>+,.0f})")
