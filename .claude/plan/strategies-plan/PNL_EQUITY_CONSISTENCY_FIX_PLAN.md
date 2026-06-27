# PnL / Equity / Risk Report Consistency Fix Plan

> Created: 2026-06-27
> Status: Investigation complete, ready to implement
> Severity: High — affects NAV accuracy, historical equity stability, report cross-consistency

---

## 1. Problem Statement

Three systems compute PnL/equity independently but are expected to agree:

| System | Output | Price Source | Dividend Adj? | Scale Factor? |
|--------|--------|-------------|--------------|---------------|
| **UpdateStrategyPerformance** | `strategy_performance.json` (authoritative NAV) | MongoDB `c` + `adjust_price_df` | **No** | No (sim-capital, then equity-level regime scale) |
| **DailySignal monitor** | `daily_report.json` `position_monitor` + `monitor_log` in inventory | MongoDB → Polygon → Yahoo waterfall, `Adj Close` | **Yes** | **Yes** (`upnl × scale_factor`) |
| **PnLReport** | `pnl_report_*.pdf` | yfinance `auto_adjust` (summary) / MongoDB `c` (equity curve) | Mixed | Mixed |
| **RiskManager** | `risk_report_*.pdf` | PriceDataStore Parquet (`Adj Close`) | **Yes** | No (raw inventory shares) |

Observed symptoms:
- `strategy_performance.json` 6/23 equity changed from $1,141,400 → $1,134,989 (-$6,411) after cron re-ran
- PnL report vs Risk report unrealized diff of $2-11k on some dates
- PnL report internally inconsistent: CLOSE PnL (scaled) vs HOLD unrealized (unscaled) in equity curve

---

## 2. Root Causes (5 defects)

### Defect A: Non-idempotent full-history recomputation

**Location:** `DailySignal.py` L2218
```python
_perf_start = '2026-03-19'   # HARDCODED
subprocess.run(f'python UpdateStrategyPerformance.py --start {_perf_start} --end {_perf_end}'.split(), ...)
```

**Problem:** Every cron run recomputes ALL dates from 3/19 to today. Any change in:
- `splits_cache.json` (rebuilt from Polygon daily → may add/modify split entries)
- MongoDB prices (feeder may backfill corrections)
- Regime weight (fixed-weight mode uses `--end` date's weight for ALL dates)

...causes historical equity values to silently change.

**Impact:** ~$1,038 of the $6,411 diff comes from regime weight alone (0.552→0.557 between 6/23 and 6/25 runs). ~$5,373 from `splits_cache.json` change affecting `adjust_position_view` on 6/12+.

### Defect B: Fixed-weight mode rewrites history

**Location:** `UpdateStrategyPerformance.py` L376-382
```python
if weight_mode == "fixed":
    rw = fixed_rw   # ← uses --end date's regime weight for ALL dates
mrpt_real = rw["mrpt_capital"] * (mrpt_sim / SIM_CAPITAL)
```

**Problem:** Default `--fixed` mode takes the latest regime weight and applies it to every historical date. So 3/19 equity is recomputed with 6/25's regime weight, not 3/19's actual weight. Each day the cron runs, all historical values shift.

### Defect C: scale_factor leaks into realized PnL

**Location:** `DailySignal.py` L611
```python
return round(upnl * scale_factor, 2), upnl_pct
```

This scaled value is written to:
1. `monitor_log[].unrealized_pnl` in inventory (L1533: `upnl = round(upnl * scale_factor, 2)`)
2. `daily_report.json` → `position_monitor[].unrealized_pnl`

Then consumed by:
- **PnLReport equity curve** (L786): reads CLOSE PnL from `daily_report` → uses scaled value directly
- **PnLReport equity curve** (L873): computes HOLD MTM from `inventory_shares × close_price` → **unscaled**
- **UpdateStrategyPerformance `compute_pnl_monitor` fallback** (L152-157): reads `monitor_log.unrealized_pnl` → **scaled**, but caller `reconstruct_equity` treats it as sim-capital basis

**Impact:** When `scale_factor ≠ 1.0` (i.e., regime capital ≠ sim_capital), realized PnL (scaled) and unrealized PnL (unscaled) are on different scales within the same equity curve.

### Defect D: Dividend adjustment inconsistency

**Location comparison:**

| Function | File:Line | Dividend Adjusted? |
|----------|----------|-------------------|
| `load_prices_mongo` | UpdateStrategyPerformance.py:61 | **No** (raw `c` field) |
| `download_prices_mongo` | PnLReport.py:519 | **No** (raw `c` field) |
| `load_historical_data_mongo` | PortfolioMRPTRun.py:680 | **Yes** (`Adj Close` via dividend factor) |
| PriceDataStore | RiskManager.py:203 | **Yes** (`Adj Close`) |

**Problem:** Monitor sees dividend-adjusted prices, UpdateSP sees raw close. For a stock that pays a $1 dividend on date D:
- Monitor: `Adj Close` before D is retroactively lowered → unrealized PnL changes
- UpdateSP: `c` (raw close) before D is unchanged → no PnL change

For the current portfolio (MRPT/MTFS pairs, mostly growth stocks), dividend impact is small ($100s) but nonzero and accumulates.

### Defect E: `compute_pnl_monitor` fallback uses scaled values

**Location:** `UpdateStrategyPerformance.py` L152-157
```python
def compute_pnl_monitor(pos, date_str):
    for entry in reversed(pos.get("monitor_log", [])):
        if entry.get("date") == date_str and entry.get("unrealized_pnl") is not None:
            return entry["unrealized_pnl"], "monitor_log"
```

**Problem:** `monitor_log[].unrealized_pnl` already has `scale_factor` baked in (written by DailySignal L1533). But `reconstruct_equity` (L223) adds it to `SIM_CAPITAL (500k)`:
```python
equity = SIM_CAPITAL + cumulative_realized + total_unrealized
```
If `scale_factor = 0.93`, the monitor PnL is 0.93× of what it should be at sim-capital basis. This makes the sim equity wrong whenever the MongoDB price path fails and the fallback fires.

---

## 3. Proposed Fixes

### Fix 1: Incremental-only UpdateStrategyPerformance (addresses Defects A, B)

**Goal:** Only compute equity for NEW dates, never rewrite historical values.

**Changes to `DailySignal.py` L2218:**
```python
# BEFORE (recomputes all history):
_perf_start = '2026-03-19'

# AFTER (incremental: only compute today):
_perf_start = signal_date.strftime('%Y-%m-%d')
```

**Changes to `UpdateStrategyPerformance.py`:**

1. **L316-320 (main function):** When `--start == --end` (single-day mode), load the prior day's equity from the existing `strategy_performance.json` instead of recomputing from scratch:
   ```python
   if start == end:
       # Incremental mode: load prior equity, compute only today's delta
       prior_record = load_prior_from_perf_json(start)
       # ... compute today's PnL only
       # ... append to existing JSON instead of replace
   ```

2. **Regime weight handling:** Use `--daily-weights` mode by default (each date uses its own signal_date's regime weight), or store the regime weight at the time of computation and never overwrite.

3. **New CLI option `--append-only`:** Prevents overwriting existing dates. Only adds new dates or updates the latest date.

**Risk:** If a backfill/correction is needed, provide a separate `--force-recompute` flag that explicitly allows full recomputation (but this should never run in daily cron).

### Fix 2: Remove scale_factor from stored PnL (addresses Defects C, E)

**Goal:** All stored PnL values are on sim-capital basis (500k). Regime scaling happens only at the final equity→real_equity step.

**Changes to `DailySignal.py`:**

1. **L611 (`_compute_close_upnl`):** Return unscaled value:
   ```python
   # BEFORE:
   return round(upnl * scale_factor, 2), upnl_pct
   # AFTER:
   return round(upnl, 2), upnl_pct
   ```

2. **L1533 (HOLD monitor_log write):** Same — remove scale_factor:
   ```python
   # BEFORE:
   upnl = round(upnl * scale_factor, 2)
   # AFTER:
   upnl = round(upnl, 2)
   ```

3. **L1663 (signal dict unrealized_pnl):** Same pattern:
   ```python
   # BEFORE:
   sig['unrealized_pnl'] = round(upnl * scale_factor, 2)
   # AFTER:
   sig['unrealized_pnl'] = round(upnl, 2)
   ```

4. **Add `scale_factor` as a separate field** in monitor output for reporting:
   ```python
   sig['scale_factor'] = scale_factor
   sig['unrealized_pnl_scaled'] = round(upnl * scale_factor, 2)  # for display only
   ```

**Impact on downstream:**
- `UpdateStrategyPerformance.compute_pnl_monitor` fallback: now returns sim-capital basis → consistent with `compute_pnl_mongo`
- `PnLReport` equity curve: CLOSE events from `daily_report` are now unscaled → consistent with HOLD MTM
- `PnLReport` summary display: needs to decide whether to show scaled or unscaled PnL (recommend: show scaled for user-facing, use unscaled internally)

**Migration:** Historical `monitor_log` entries in `inventory_history/` and `daily_report_*.json` files already written with scale_factor baked in. Options:
- (a) Leave historical data as-is, add a `_pnl_scaled: true` marker to old entries, handle both formats in readers
- (b) One-time migration script to un-scale all historical entries (cleaner but risky)
- Recommend (a) for safety

### Fix 3: Unify price source — use MongoDB close everywhere (addresses Defect D)

**Goal:** All PnL computations use the same price: MongoDB `c` with `adjust_price_df` (split-adjusted, no dividend adjustment).

**Rationale:** The authoritative NAV (`strategy_performance.json`) already uses this price source. Making everything else match eliminates dividend-adjustment divergence.

**Changes:**

1. **`DailySignal._compute_close_upnl` (L586-611):** Currently uses `prices_today` from simulation's `Adj Close`. Change to use MongoDB close price for the signal_date:
   ```python
   # Load signal_date close from MongoDB (same as UpdateSP)
   from UpdateStrategyPerformance import load_prices_mongo
   mongo_prices = load_prices_mongo([s1, s2], signal_date, signal_date)
   s1_price = mongo_prices.get(s1, {}).get(signal_date_str) or prices_today.get(s1)
   s2_price = mongo_prices.get(s2, {}).get(signal_date_str) or prices_today.get(s2)
   ```

   **Alternative (simpler):** Keep using `prices_today` from the simulation but document the known dividend divergence as acceptable (~$100s per position).

2. **`RiskManager.current_unrealized`:** Currently uses PriceDataStore `Adj Close`. Change to use `price_on(signal_date)` which is the same PriceDataStore but at least date-pinned. The dividend difference vs MongoDB is small and acceptable for risk monitoring purposes.

3. **`PnLReport` summary:** Currently uses yfinance `auto_adjust`. Change to use MongoDB close (same as equity curve). This eliminates the internal sanity-check warning at L1305.

**Risk:** Monitor's exit signals (stop loss, momentum decay) use `Adj Close` for decision-making. Changing the price source for PnL reporting is separate from signal generation — signals should continue using `Adj Close` for correct behavior.

### Fix 4: Lock splits_cache for historical dates (addresses Defect A partially)

**Goal:** Prevent `splits_cache.json` changes from affecting historical PnL.

**Changes to `UpdateStrategyPerformance.py`:**

1. When computing PnL for date D, use the `splits_cache` state as of date D, not today's cache.
2. Practically: pin `adjust_position_view` and `adjust_price_df` to only apply splits with `execution_date <= D` (already mostly done, but verify edge cases).
3. Alternatively: store a per-date hash of splits applied, and warn if it changes on re-run.

---

## 4. Implementation Order

| Phase | Fix | Effort | Risk | Dependency |
|-------|-----|--------|------|------------|
| **Phase 1** | Fix 2 (remove scale_factor from stored PnL) | Medium | Medium — affects all downstream readers | None |
| **Phase 2** | Fix 1 (incremental UpdateSP) | Medium | Low — additive, doesn't break existing | Fix 2 (ensures fallback consistency) |
| **Phase 3** | Fix 3 (unify price source) | High | High — touches 4 files | Fix 2 |
| **Phase 4** | Fix 4 (lock splits_cache) | Low | Low | None |

**Recommended start:** Fix 2 (scale_factor) — highest impact, most clearly wrong, and unblocks Fix 1.

---

## 5. Verification Criteria

After each fix, verify:

### 5a. Internal consistency (per report)
- PnL report: `已实现 + 未实现 = 小计` for MRPT, MTFS, 合计
- PnL report: MRPT 已实现 monotonically non-decreasing across consecutive reports
- Risk report: NAV matches `strategy_performance.json` combined_equity for the same date

### 5b. Cross-report alignment (PnL vs Risk)
- For each date: `|PnL_unrealized - Risk_unrealized| < $2,000`
- For current-day report: diff should be < $500

### 5c. Stability (idempotency)
- Run `UpdateStrategyPerformance.py --start X --end Y` twice with the same inputs → identical output
- Run cron on consecutive days → historical dates in `strategy_performance.json` unchanged

### 5d. Scale-factor consistency
- `daily_report.json` `unrealized_pnl` values should match `inventory_shares × (close - open)` exactly (no scale_factor)
- `strategy_performance.json` equity should equal `SIM_CAPITAL + Σrealised + Σunrealised` (sim basis), then `× regime_capital / SIM_CAPITAL` for real equity

---

## 6. Files Affected

| File | Fix 1 | Fix 2 | Fix 3 | Fix 4 |
|------|-------|-------|-------|-------|
| `DailySignal.py` | L2218 | L611, L1533, L1663 | L586-611 | — |
| `UpdateStrategyPerformance.py` | L316-400 | — | L61-92 | L84-90, L143 |
| `PnLReport.py` | — | L786, L790 | L519-553, L556-561 | — |
| `RiskManager.py` | — | — | L248-253 | — |
| `PortfolioMRPTRun.py` | — | — | (no change, signals stay on Adj Close) | — |
| `PortfolioMTFSRun.py` | — | — | (no change) | — |

---

## 7. Appendix: Price Source Reference

```
MongoDB stock_data.c ──→ adjust_price_df ──→ Split-adjusted close (NO dividend)
                                              ├── UpdateStrategyPerformance (L78-90)
                                              ├── PnLReport equity curve (L519-553)
                                              └── PnLReport MTM for HOLD (L873)

PortfolioRun.load_historical_data_mongo ──→ Adj Close (split + dividend adjusted)
                                              ├── DailySignal monitor prices_today (L1634)
                                              └── DailySignal _compute_close_upnl (L606)

PriceDataStore (Polygon Parquet) ──→ Adj Close (split + dividend adjusted)
                                              └── RiskManager current_price (L248)

yfinance auto_adjust=True ──→ Adj Close (split + dividend adjusted)
                                              └── PnLReport summary section (L556-561)
```
