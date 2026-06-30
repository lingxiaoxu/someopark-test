# Cross-Day Cooling Period Implementation Plan

> Created: 2026-06-29

## 1. Problem

HAS/PFE traded 6 times (4 losses), HAS/YUM re-entered next day after profitable close.
No cross-day cooling exists in live trading.

## 2. Cooling Rules

| Close Type | Cooling (trading days) |
|-----------|----------------------|
| Profit (pnl >= 0) | 1 trading day |
| Loss (pnl < 0) | 3 trading days |

MRPT and MTFS use identical rules. Anti-churn (same-day) kept as-is, no overlap.

## 3. Constants

File: `DailySignal.py` top-level (~L95)

```python
_COOLING_PROFIT_DAYS = 1    # trading days to cool after profitable close
_COOLING_LOSS_DAYS   = 3    # trading days to cool after loss close
```

## 4. New Utility Function

File: `DailySignal.py`, after `prev_weekday()` (~L131)

```python
def trading_days_between(d1, d2) -> int:
    """Count NYSE trading days strictly between d1 and d2 (exclusive both ends).
    Returns 0 if d2 <= d1 or same/adjacent trading day."""
    try:
        import pandas_market_calendars as mcal
        t1 = pd.Timestamp(d1)
        t2 = pd.Timestamp(d2)
        if t2 <= t1:
            return 0
        nyse = mcal.get_calendar('NYSE')
        valid = nyse.valid_days(
            (t1 + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
            (t2 - pd.Timedelta(days=1)).strftime('%Y-%m-%d'))
        return len(valid)
    except Exception:
        # Fallback: weekday count
        count = 0
        d = pd.Timestamp(d1) + pd.Timedelta(days=1)
        end = pd.Timestamp(d2)
        while d < end:
            if d.weekday() < 5:
                count += 1
            d += pd.Timedelta(days=1)
        return count
```

## 5. Preserve Close Metadata in Inventory

File: `DailySignal.py` L1236-1237

**Before:**
```python
elif action in ('CLOSE', 'CLOSE_STOP'):
    inv['pairs'][pair] = {'direction': None}
```

**After:**
```python
elif action in ('CLOSE', 'CLOSE_STOP'):
    _prev = inv['pairs'].get(pair, {})
    inv['pairs'][pair] = {
        'direction': None,
        'last_close_date': signal_date,
        'last_close_pnl': sig.get('unrealized_pnl', 0) or 0,
        'last_close_action': action,
        'last_close_param_set': _prev.get('param_set'),
    }
```

Notes:
- `signal_date` already a param of `update_inventory_from_signals(inv, signals, signal_date)` at L1207
- `_prev.get('param_set')` reads from still-active position before overwrite
- Old entries `{'direction': None}` without `last_close_date` are compatible (cooling skips them)

## 6. Cross-Day Cooling Check

File: `DailySignal.py`, in `extract_signals()`, insert after anti-churn (L1055) before Fix 6A (L1057)

```python
        # Cross-day cooling: block re-open of pairs closed within cooling window.
        # Profit close: 1 trading day.  Loss close: 3 trading days.
        # Complements anti-churn (same-day only) without overlap.
        if sig.get('action') in ('OPEN_LONG', 'OPEN_SHORT'):
            _inv_pair = inventory.get('pairs', {}).get(pair_key, {})
            _lcd = _inv_pair.get('last_close_date')
            if _lcd:
                _sig_date_str = str(signal_ts.date()) if hasattr(signal_ts, 'date') else str(signal_ts)
                _td = trading_days_between(_lcd, _sig_date_str)
                _lcd_pnl = _inv_pair.get('last_close_pnl', 0) or 0
                _cool = _COOLING_PROFIT_DAYS if _lcd_pnl >= 0 else _COOLING_LOSS_DAYS
                if _td < _cool:
                    original_action = sig['action']
                    sig['action'] = 'MACRO_VETO'
                    sig['original_action'] = original_action
                    sig['note'] = (
                        f"Cross-day cooling -- {pair_key} closed {_td} trading day(s) ago "
                        f"(pnl=${_lcd_pnl:+,.0f}, cooling={_cool}d)"
                    )
                    log.info(f"[COOLING] {pair_key}: vetoed {original_action} -- "
                             f"closed {_lcd} ({_td}td ago, need {_cool}td)")
```

## 7. Anti-churn vs Cooling: No Overlap

| Mechanism | When | Trigger | Duration | Change |
|-----------|------|---------|----------|--------|
| anti-churn | Day T Step1->Step2 | Same-day loss close | One run | Keep as-is |
| cooling | Day T+1 onward | Any close | 1-3 trading days | New |

Same-day: anti-churn handles it. Next day+: cooling handles it. No double-block.

## 8. Test Scenarios

**HAS/YUM profit close (+$4,396) on 6/24 (Tue):**
- 6/25 (Wed): td_between = 0 < 1 -> BLOCKED
- 6/26 (Thu): td_between = 1 >= 1 -> ALLOWED

**HAS/PFE loss close (-$1,884) on 6/11 (Wed):**
- 6/12 (Thu): td = 0 < 3 -> BLOCKED
- 6/13 (Fri): td = 1 < 3 -> BLOCKED
- 6/16 (Mon): td = 2 < 3 -> BLOCKED (weekend skipped)
- 6/17 (Tue): td = 3 >= 3 -> ALLOWED

**Same-day loss close:**
- Day T: anti-churn blocks Step 2
- Day T+1: cooling blocks (td=0 < 3)
- Day T+4: cooling allows (td=3 >= 3)

**Old inventory (no last_close_date):** skip check, allowed.

## 9. Files Changed

`DailySignal.py` only, 4 locations:
1. ~L95: `_COOLING_PROFIT_DAYS`, `_COOLING_LOSS_DAYS` constants
2. ~L131: `trading_days_between()` function
3. L1236-1237: preserve close metadata in inventory
4. ~L1056: cross-day cooling check block

## 10. NOT Changed

- `PortfolioClasses.py` backtest cooling_off logic
- `PortfolioMRPTStrategyRuns.py` / `PortfolioMTFSStrategyRuns.py` param values
- `churn_blocked_pairs` mechanism (L2136-2162, L1046-1055)
- `PnLReport.py` / `RiskManager.py` / `UpdateStrategyPerformance.py`

## 11. Log Output

```
[COOLING] HAS/YUM: vetoed OPEN_SHORT -- closed 2026-06-24 (0td ago, need 1td)
[COOLING] HAS/PFE: vetoed OPEN_LONG -- closed 2026-06-11 (2td ago, need 3td)
```
