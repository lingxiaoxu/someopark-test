# PnL / Equity / Risk Report Consistency Fix Plan

> Created: 2026-06-27 | Audited: 2026-06-27
> Status: Investigation complete, audit passed, ready to implement
> Severity: High — affects NAV accuracy, historical equity stability, report cross-consistency

---

## 1. Problem Statement

Three systems compute PnL/equity independently but are expected to agree:

| System | Output | Price Source | Dividend Adj? | Scale Factor? |
|--------|--------|-------------|--------------|---------------|
| **UpdateStrategyPerformance** | `strategy_performance.json` (authoritative NAV) | MongoDB `c` + `adjust_price_df` | **No** | No (sim-capital, then equity-level regime scale) |
| **DailySignal monitor** | `daily_report.json` `position_monitor` + `monitor_log` in inventory | MongoDB → Polygon → Yahoo waterfall, `Adj Close` via `load_historical_data_mongo` | **Yes** | **Yes** (`upnl × scale_factor`) |
| **PnLReport** | `pnl_report_*.pdf` | yfinance `auto_adjust` (summary) / MongoDB `c` (equity curve) | Mixed | Mixed — HOLD un-scaled at L790, CLOSE left scaled at L786 |
| **RiskManager** | `risk_report_*.pdf` | PriceDataStore Parquet (`Adj Close`) | **Yes** | No (raw inventory shares) |

Observed symptoms:
- `strategy_performance.json` 6/23 equity changed from $1,141,400 → $1,134,989 (-$6,411) after cron re-ran
- PnL report vs Risk report unrealized diff of $2-11k on some dates
- PnL report internally inconsistent: CLOSE PnL (scaled) vs HOLD unrealized (unscaled) in equity curve

---

## 2. Root Causes (5 defects)

### Defect A: Non-idempotent full-history recomputation

**Location:** `DailySignal.py` L2217-2220
```python
_perf_start = '2026-03-19'   # HARDCODED
subprocess.run(f'python UpdateStrategyPerformance.py --start {_perf_start} --end {_perf_end}'.split(),
               capture_output=True, timeout=120)
```

**Problem:** Every cron run recomputes ALL dates from 3/19 to today. Any change in:
- `splits_cache.json` (rebuilt from Polygon API daily at `CorporateActions.fetch_all_splits` L157-197 → may add/modify split entries)
- MongoDB prices (feeder may backfill corrections)
- Regime weight (fixed-weight mode uses `--end` date's weight for ALL dates)

...causes historical equity values to silently change.

**Impact:** ~$1,038 of the $6,411 diff comes from regime weight alone (mrpt 0.552→0.557 between 6/23 and 6/25 runs). ~$5,373 from `splits_cache.json` / `adjust_position_view` change affecting 6/12+ (KLAC split boundary).

### Defect B: Fixed-weight mode rewrites history

**Location:** `UpdateStrategyPerformance.py` L364-382
```python
fixed_rw = get_regime_for_date(regime_weights, args.end)  # L366
# ... later, for EVERY date:
if weight_mode == "fixed":
    rw = fixed_rw                                          # L377
mrpt_real = rw["mrpt_capital"] * (mrpt_sim / SIM_CAPITAL)  # L381
```

**Problem:** Default `--fixed` mode takes the `--end` date's regime weight and applies it to every historical date. So 3/19 equity is recomputed with 6/25's regime weight, not 3/19's actual weight. Each day the cron runs with a new `--end`, all historical values shift.

### Defect C: scale_factor leaks into stored PnL

**Location:** `DailySignal.py` — three write sites:
- L611: `return round(upnl * scale_factor, 2), upnl_pct` — CLOSE/CLOSE_STOP PnL
- L1533: `upnl = round(upnl * scale_factor, 2)` — HOLD monitor_log
- L1663: `sig['unrealized_pnl'] = round(upnl * scale_factor, 2)` — HOLD signal dict

These scaled values flow to:
1. `monitor_log[].unrealized_pnl` in inventory snapshots
2. `daily_report.json` → `position_monitor[].unrealized_pnl`

Then consumed by downstream with **mixed treatment**:
- **PnLReport equity curve L786**: reads CLOSE PnL from `daily_report` → **uses scaled value directly**
- **PnLReport equity curve L790**: reads HOLD PnL → **un-scales it** (`raw_upnl = upnl / _scale[strat]`)
- **PnLReport equity curve L873**: computes HOLD MTM from `inventory_shares × close_price` → **unscaled**
- **PnLReport summary L1010**: CLOSE `sys_pnl = close_ev['pnl']` → **scaled**
- **PnLReport summary L1052**: HOLD `sys_pnl = mtm_pnl` → **unscaled**
- **UpdateStrategyPerformance `compute_pnl_monitor` fallback L155**: reads `monitor_log.unrealized_pnl` → **scaled**, but `reconstruct_equity` L223 treats it as sim-capital basis

**Verified with real data (6/24, MTFS scale_factor=0.886):**
- 8 HOLD positions: total scaled = -$33,767, total unscaled = -$38,111. **Diff = $4,345/day**
- 3 CLOSE_STOP events: total scaled = -$23,855, total unscaled = -$26,925. **Diff = $3,069 locked permanently as realized**

**Impact:** When `scale_factor ≠ 1.0`, the PnL report equity curve has a **discontinuity on position close day** — unrealized at 1.0x abruptly becomes realized at 0.886x. Summary table mixes scaled CLOSE PnL with unscaled HOLD MTM.

### Defect D: Dividend adjustment inconsistency

**Location comparison:**

| Function | File:Line | Dividend Adjusted? |
|----------|----------|-------------------|
| `load_prices_mongo` | UpdateStrategyPerformance.py:61-92 | **No** (raw `c` field) |
| `download_prices_mongo` | PnLReport.py:519-553 | **No** (raw `c` field) |
| `load_historical_data_mongo` | PortfolioMRPTRun.py:680-824 | **Yes** (`Adj Close` via backward cumulative dividend factor) |
| PriceDataStore | RiskManager.py:203-210 | **Yes** (`Adj Close`) |

**Impact:** Small. Verified with current portfolio: IRM/MSFT most impactful at ~$302/month dividend gap. All 8 MTFS pairs combined: ~$200-800/month. Most positions held days to weeks → per-position impact $10-100. Dwarfed by Defect C ($3,000-4,000/day).

### Defect E: `compute_pnl_monitor` fallback uses scaled values

**Location:** `UpdateStrategyPerformance.py` L152-157

**Problem:** `monitor_log[].unrealized_pnl` has `scale_factor` baked in (from DailySignal L1533). When `compute_pnl_mongo` fails (MongoDB price missing), `compute_pnl_monitor` returns the scaled value, but `reconstruct_equity` L223 adds it to `SIM_CAPITAL (500k)` as if it were sim-capital basis. If `scale_factor = 0.886`, the PnL is 11.4% too small.

**Probability:** Low — MongoDB rarely has gaps for S&P 500 stocks. But when it fires, the impact is proportional to the position's PnL.

---

## 3. Proposed Fixes

### Fix 1: Freeze historical dates in UpdateStrategyPerformance (addresses Defects A, B)

**Goal:** Daily cron only computes/updates the latest few dates, never rewrites old history.

> **Audit note:** The original "incremental-only" approach is NOT feasible because `strategy_performance.json` does not store `cumulative_realized` — only total equity. Cannot reconstruct the realized/unrealized breakdown from equity alone. Redesigned as "freeze old, recompute recent" approach.

**Changes to `UpdateStrategyPerformance.py`:**

1. **New `--freeze-before` CLI option (default: 5 trading days ago):**
   ```python
   parser.add_argument('--freeze-before', default=None,
       help='Do not overwrite dates before this (YYYY-MM-DD). Default: 5 trading days before --end.')
   ```

2. **Main function (L362-393):** After building `real_records`, filter:
   ```python
   if args.freeze_before:
       freeze = args.freeze_before
   else:
       freeze = get_nth_prev_trading_day(args.end, 5)
   # Only write records on or after freeze date
   records_to_write = [r for r in real_records if r['date'] >= freeze]
   ```

3. **Merge logic (L404-420):** When updating `strategy_performance.json`, only overwrite dates >= `freeze_before`. Dates before that are preserved as-is from the existing file.

4. **Regime weight:** Switch cron invocation to `--daily-weights` mode:
   ```python
   # DailySignal.py L2218:
   _perf_cmd = f'python UpdateStrategyPerformance.py --start {_perf_start} --end {_perf_end} --daily-weights'
   ```
   Each date uses its own signal_date's regime weight → no retroactive weight changes.

5. **Explicit `--force-full-recompute` flag** for manual backfill/corrections (never used by cron).

**Risk:** If a correction is needed for old dates, operator must manually use `--force-full-recompute`. This is acceptable since corrections should be deliberate.

### Fix 2: Remove scale_factor from stored PnL (addresses Defects C, E)

**Goal:** All stored PnL values are on sim-capital basis (500k). Regime scaling happens only at the final equity→real_equity step.

**Changes to `DailySignal.py`:**

1. **L611 (`_compute_close_upnl` return):**
   ```python
   # BEFORE:
   return round(upnl * scale_factor, 2), upnl_pct
   # AFTER:
   return round(upnl, 2), upnl_pct
   ```

2. **L1533 (HOLD monitor_log write):**
   ```python
   # BEFORE:
   upnl = round(upnl * scale_factor, 2)
   # AFTER:
   upnl = round(upnl, 2)
   ```

3. **L1663 (signal dict unrealized_pnl):**
   ```python
   # BEFORE:
   sig['unrealized_pnl'] = round(upnl * scale_factor, 2)
   # AFTER:
   sig['unrealized_pnl'] = round(upnl, 2)
   ```

4. **Add `scale_factor` as metadata** in signal output for display:
   ```python
   sig['scale_factor'] = scale_factor
   sig['unrealized_pnl_scaled'] = round(upnl * scale_factor, 2)  # for user-facing display only
   ```

5. **L1780-1784 (`peak_unrealized_pnl` update):** Currently uses scaled PnL for MTFS trailing profit stop. After fix, `peak_unrealized_pnl` will be unscaled. **Must verify trailing stop logic still works** — the comparison is `current_upnl vs peak * retain_pct`, so as long as both are on the same scale (both unscaled), the stop triggers at the same relative level. **No code change needed** but add a comment.

**Critical downstream changes:**

6. **PnLReport.py L790 — MUST stop un-scaling:**
   ```python
   # BEFORE (divides by scale_factor to un-scale):
   raw_upnl = upnl / _scale[strat]
   # AFTER (value is already unscaled):
   raw_upnl = upnl
   ```
   > **⚠️ Double-correction risk:** If L790 still divides by `_scale[strat]` after Fix 2 stores unscaled values, HOLD PnL in the equity curve will be double-un-scaled. This is the most critical downstream change.

7. **PnLReport.py L786 — CLOSE PnL now consistent:**
   ```python
   pnl = e.get('unrealized_pnl', 0) or 0  # now unscaled, consistent with HOLD MTM
   ```
   No code change needed, but the value changes scale.

8. **PnLReport summary (L1010, L1052):** After fix, both CLOSE and HOLD `sys_pnl` are unscaled. For user-facing display, multiply by `scale_factor` if scaled PnL is desired.

**Migration for historical data:**

Option (a) — **Recommended**: Leave historical `daily_report_*.json` and `inventory_history/` files as-is. Add a format marker:
- New entries (after fix): `"_pnl_version": 2` (unscaled)
- Old entries (before fix): no marker = version 1 (scaled)

Readers must check: if `_pnl_version` absent or < 2, divide `unrealized_pnl` by `scale_factor` to get sim-capital basis before using.

**Affected readers (4 total):**
1. PnLReport equity curve (L786, L790)
2. PnLReport summary (L1010)
3. UpdateStrategyPerformance `compute_pnl_monitor` fallback (L155)
4. DailySignal `peak_unrealized_pnl` update (L1780-1784) — only reads same-run entries, always version 2 after fix

### Fix 3: Unify price source — use MongoDB close everywhere (addresses Defect D)

**Goal:** All PnL computations use the same price: MongoDB `c` with `adjust_price_df` (split-adjusted, no dividend adjustment).

**Rationale:** The authoritative NAV (`strategy_performance.json`) already uses this price source. Making everything else match eliminates dividend-adjustment divergence.

**Changes:**

1. **`DailySignal._compute_close_upnl` (L586-611):** Currently uses `prices_today` from simulation's `Adj Close` (dividend-adjusted). Change to use MongoDB close:
   ```python
   from db.connection import get_main_db
   def _get_mongo_close(ticker, date_str):
       """Get split-adjusted close from MongoDB for PnL consistency."""
       db = get_main_db()
       doc = db.stock_data.find_one({'symbol': ticker, 't': {'$gte': ts_start, '$lt': ts_end}})
       return doc['c'] if doc else None
   ```
   With split adjustment via `adjust_price_df` or lookup from the already-loaded mongo prices.

   **Alternative (simpler, recommended for Phase 3):** Keep using `prices_today` from simulation. The dividend divergence is $10-100 per position per close event, acceptable. Document as known limitation.

2. **`RiskManager.current_unrealized` (L627-638):** Uses PriceDataStore `Adj Close`. The dividend difference vs MongoDB is small (~$100s). Acceptable for risk monitoring. No change needed unless perfect alignment required.

3. **`PnLReport` summary (L556-561):** Uses yfinance `auto_adjust`. Change to use MongoDB close (same as equity curve L519-553). This eliminates the internal sanity-check warning at L1305-1310.

**Risk:** Monitor's exit signals (stop loss, momentum decay) MUST continue using `Adj Close` for correct behavior. Only the PnL _reporting_ changes — signal generation is unaffected.

### Fix 4: Lock splits_cache for historical dates (addresses Defect A partially)

**Goal:** Prevent `splits_cache.json` changes from affecting historical PnL.

**Changes to `UpdateStrategyPerformance.py`:**

1. `adjust_position_view` already checks `execution_date <= today` (CorporateActions.py L613). Verify that `adjust_price_df` also respects this bound (L236: `if ed > today_str: continue` — confirmed).

2. **New: pin `adjust_price_df` to signal_date, not today:**
   ```python
   # In load_prices_mongo, pass effective_date to adjust_price_df:
   adjust_price_df(df, ticker, effective_date=end_date)
   ```
   Where `effective_date` replaces `today_str` in the `ed > today_str` check. This ensures that when recomputing 6/10, only splits executed by 6/10 are applied — not future splits that happened to be in the cache.

3. **Add `adjust_price_df` `effective_date` parameter** to `CorporateActions.py` L215:
   ```python
   def adjust_price_df(df, ticker, splits=None, effective_date=None, ...):
       today_str = effective_date or str(date.today())
   ```

---

## 4. Implementation Order

| Phase | Fix | Effort | Risk | Dependency |
|-------|-----|--------|------|------------|
| **Phase 1** | Fix 2 (remove scale_factor from stored PnL) | Medium | Medium — 4 downstream readers need update | None |
| **Phase 2** | Fix 1 (freeze historical + daily-weights) | Medium | Low — additive, preserves existing | Fix 2 (ensures fallback consistency) |
| **Phase 3** | Fix 4 (lock splits_cache via effective_date) | Low | Low — parameter addition only | None (can be done in parallel) |
| **Phase 4** | Fix 3 (unify price source) | Low-Medium | Low — small dollar impact, can defer | Fix 2 |

**Recommended start:** Fix 2 (scale_factor) — highest dollar impact (~$3-4k/day), most clearly wrong, and unblocks Fix 1.

---

## 5. Verification Criteria

After each fix, verify:

### 5a. Internal consistency (per report)
- PnL report: `已实现 + 未实现 = 小计` for MRPT, MTFS, 合计
- PnL report: 已实现 monotonically non-decreasing across consecutive reports (both strategies)
- Risk report: NAV matches `strategy_performance.json` combined_equity for the same date

### 5b. Cross-report alignment (PnL vs Risk)
- For each date: `|PnL_unrealized - Risk_unrealized| < $2,000`
- For current-day report: diff should be < $500

### 5c. Stability (idempotency)
- Run `UpdateStrategyPerformance.py --start X --end Y` twice with the same inputs → identical output
- Run cron on consecutive days → historical dates (before freeze window) in `strategy_performance.json` unchanged
- Verify with: `diff <(python UpdateSP --start ... --end ... --dry-run) <(python UpdateSP --start ... --end ... --dry-run)`

### 5d. Scale-factor consistency
- `daily_report.json` `unrealized_pnl` = `inventory_shares × (close - open)` exactly (no scale_factor)
- `daily_report.json` also has `unrealized_pnl_scaled` = above × scale_factor (for display)
- `strategy_performance.json` equity = `SIM_CAPITAL + Σrealized + Σunrealized` (sim basis), then `× regime_capital / SIM_CAPITAL` for real equity

### 5e. Trailing stop integrity (Fix 2 specific)
- MTFS trailing profit stop: `peak_unrealized_pnl` and current `unrealized_pnl` both unscaled → relative comparison unchanged
- Run backtest on 2-3 historical pairs with known trailing stop triggers, verify same trigger dates

---

## 6. Files Affected

| File | Fix 1 | Fix 2 | Fix 3 | Fix 4 |
|------|-------|-------|-------|-------|
| `DailySignal.py` | L2218 (daily-weights) | L611, L1533, L1663, L1780 (comment) | L586-611 (optional) | — |
| `UpdateStrategyPerformance.py` | L303 (new --freeze-before), L362-420 (freeze logic) | — | L61-92 (optional) | — |
| `PnLReport.py` | — | **L786, L790 (critical)**, L1010, L1052 | L556-561 (optional) | — |
| `RiskManager.py` | — | — | L627-638 (optional) | — |
| `CorporateActions.py` | — | — | — | L215 (add effective_date param) |
| `PortfolioMRPTRun.py` | — | — | (no change) | — |
| `PortfolioMTFSRun.py` | — | — | (no change) | — |

---

## 7. Appendix A: Price Source Reference

```
MongoDB stock_data.c ──→ adjust_price_df ──→ Split-adjusted close (NO dividend)
                                              ├── UpdateStrategyPerformance load_prices_mongo (L61-92)
                                              ├── PnLReport equity curve download_prices_mongo (L519-553)
                                              └── PnLReport MTM for HOLD (L873)

PortfolioRun.load_historical_data_mongo ──→ Adj Close (split + dividend adjusted)
                                              ├── DailySignal monitor prices_today (L1634)
                                              └── DailySignal _compute_close_upnl (L606)

PriceDataStore (Polygon Parquet) ──→ Adj Close (split + dividend adjusted)
                                              └── RiskManager current_price (L248)

yfinance auto_adjust=True ──→ Adj Close (split + dividend adjusted)
                                              └── PnLReport summary section (L556-561)
```

## 8. Appendix B: Discrepancy Decomposition ($6,411 on 6/23)

| Source | Amount | Fix |
|--------|--------|-----|
| Regime weight change (0.552→0.557 MRPT) applied to all history | ~$1,038 | Fix 1 (daily-weights + freeze) |
| `splits_cache.json` change affecting `adjust_position_view` on 6/12+ | ~$5,373 | Fix 4 (effective_date pin) |
| **Total** | **~$6,411** | |

## 9. Appendix C: Audit Trail

- **Defect C verified with real data (6/24):** MTFS scale_factor=0.886, 8 HOLD positions total diff=$4,345, 3 CLOSE events diff=$3,069
- **Defect D impact estimated:** ~$200-800/month across all pairs (dominated by IRM dividends ~$302/mo)
- **Defect E trigger probability:** Low (requires MongoDB price gap for S&P 500 stocks), but impact = 11.4% of affected position PnL when scale_factor=0.886
- **Fix 1 redesigned:** Original "incremental-only" approach infeasible (no persisted `cumulative_realized`). Replaced with "freeze old + recompute recent window" approach
- **Fix 2 double-correction risk identified:** PnLReport L790 currently divides by scale_factor to un-scale. After Fix 2, stored values are already unscaled → L790 must be changed to avoid double-correction
- **Fix 2 peak_unrealized_pnl impact:** Trailing profit stop uses relative comparison (current vs peak × retain_pct). Both values shift scale together → trigger points unchanged. No code change needed, add comment only
