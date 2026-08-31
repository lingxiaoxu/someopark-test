# ERCOT / EIA-930 grid data — the screening study (2026-08-30/31)

User-directed: mine ERCOT for inputs that strengthen the 14 markets, wire whatever
survives the way EIA/Cleveland inputs were wired, decide by backtest. This file is the
record of the screening stage — the first gate of that route — and of why nothing
reached the replay stage this round.

## What was built (all landed, all SHADOW per §7-bis)

* `ingest/ercot.py`, table `ercot_daily`, in the daily refresh lane:
  - forward accrual from the public dashboards (5-min resolution): **natural-gas burn
    and gas share** (the quantity nothing else provides), demand/capacity, RT+DAM hub
    prices, wind/solar;
  - Public API backfill (B2C auth, credentials in .env only): 151 days of
    demand/wind/solar/DAM prices;
  - **EIA-930 deep backfill** (`backfill_eia930`): ERCO demand, day-ahead demand
    forecast, and net generation by fuel (NG/wind/solar/coal/nuclear), daily,
    **2019-01-01 onward** — 16,777 rows. Source-tagged `eia_*` metrics; units never
    mixed with the dashboard MW averages.
* A test greps `model/` for the table name: the first consumer must arrive together
  with a preregistered gate.

## The screening record, stated in full (multiplicity honesty)

Four rounds, ~30 mechanism-motivated tests, all in `/tmp/dfm_verify/ercot_screen*.py`:

1. **21 summer weeks** (the row-archive window): DAM→NG r=+0.46 p=0.03,
   ΔDAM→ICSA r=−0.71 p<0.001 — looked spectacular.
2. **399 all-season weeks** (EIA-930, winters included, Uri included): every one of
   those hits collapsed — burn→NG +0.07, everything→ICSA ≈ 0.00, monthly grid→CPI ≈ 0.
   The summer hits were single-season common-trend artifacts, caught before any wiring.
3. **Mechanism-specific shapes**: tail conditioning (|demand z|>1.5, n=54), freeze
   season (DJF), day-ahead **forecast surprise** (actual − EIA-930 DF) — all dead for
   ICSA and NG.
4. **Storage channel with a PIT day-of-year climatology** (prior-years-only, ±7d
   window): **burn z → national storage SURPRISE r=−0.352, p<0.001, n=295 — the
   physics is real and correctly signed.** But the tradeable legs are flat: NG
   print-day return r=−0.043 (p=0.47), same-week r=+0.043. The gas market prices
   Texas weather/burn before the print does.

## Verdict for the 14 markets

**No ERCOT/grid signal reached the replay stage.** KXNATGASW's price channel is dead
at weekly cadence; KXJOBLESSCLAIMS dead in linear, tail, and freeze shapes;
CPI-family dead monthly; KXWTIW never had a mechanism and tested dead; the other nine
have no channel to test. This is #184's lesson wearing a different hat: publicly
computable weather/grid information is already in the prices our markets settle
against. Wiring dead covariates into models to "see if the replay likes them" would
be theater, so the full-production PIT walk-forward replay was not spent.

## What survives, and the retest triggers

* The **burn → storage-surprise** edge (r=−0.35) predicts a number **none of our 14
  markets settles on**. If Kalshi lists an EIA-storage market, this is a ready-made
  anchor with a preregistration-ready measurement behind it.
* The ingest KEEPS RUNNING: the dashboard fuel split (true gas burn, 5-min) exists
  nowhere in EIA-930's daily rollup and accrues only forward. Retest when (a) a
  storage-settled market lists, (b) a full winter of dashboard-grade burn data
  exists, or (c) a sub-weekly (intraday) market appears where the 1-2 day EIA-930
  lag stops mattering.
* Deep zip archives (7y, hourly) remain unpulled — nothing screened so far justifies
  their cost.
