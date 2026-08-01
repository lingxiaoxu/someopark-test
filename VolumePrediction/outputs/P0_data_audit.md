# P0 数据审计报告
生成: 2026-07-26T03:13:46.757571

## 审计项 1 ✅
```json
{
 "pass": true,
 "rows": [
  {
   "date": "2019-03-01",
   "n_tickers": 8407,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2019-03-04",
   "n_tickers": 8381,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2019-03-05",
   "n_tickers": 8349,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2019-03-06",
   "n_tickers": 8360,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2019-03-07",
   "n_tickers": 8403,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2021-03-01",
   "n_tickers": 9837,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2021-03-02",
   "n_tickers": 9823,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2021-03-03",
   "n_tickers": 9845,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2021-03-04",
   "n_tickers": 9892,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2021-03-05",
   "n_tickers": 9906,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2023-03-01",
   "n_tickers": 10846,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2023-03-02",
   "n_tickers": 10881,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2023-03-03",
   "n_tickers": 10844,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2023-03-06",
   "n_tickers": 10858,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2023-03-07",
   "n_tickers": 10799,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2026-03-02",
   "n_tickers": 11965,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2026-03-03",
   "n_tickers": 11938,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2026-03-04",
   "n_tickers": 11952,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2026-03-05",
   "n_tickers": 11941,
   "fields_ok": true,
   "null_v": 0
  },
  {
   "date": "2026-03-06",
   "n_tickers": 11900,
   "fields_ok": true,
   "null_v": 0
  }
 ],
 "elapsed_s": 0.4
}
```

## 审计项 1b ❌
```json
{
 "pass": false,
 "n_obs": 310,
 "frac_within_0.5pct": 0.8935,
 "worst": [
  {
   "ticker": "HOOD",
   "date": "2025-06-06",
   "rel": 0.04805938346693593
  },
  {
   "ticker": "CRM",
   "date": "2025-06-12",
   "rel": 0.04665556685054831
  },
  {
   "ticker": "XOM",
   "date": "2025-06-13",
   "rel": 0.04395269743803583
  }
 ],
 "elapsed_s": 0.7
}
```

## 审计项 2 ✅
```json
{
 "pass": true,
 "n_429": 0,
 "pace_req_s": "3.1",
 "throttle_used": 0.12,
 "elapsed_s": 0.0
}
```

## 审计项 3 ✅
```json
{
 "pass": true,
 "KLAC": {
  "n_splits_applied": 1,
  "dollar_vol_invariant": true,
  "eta_max_around_split": 0.296
 },
 "reverse_split_case": {
  "ticker": "WHLR",
  "date": "2026-07-28",
  "n": 0,
  "dollar_vol_invariant": true
 },
 "elapsed_s": 2.2
}
```

## 审计项 4 ✅
```json
{
 "pass": true,
 "agree_rate": null,
 "n": 0,
 "n_symbols_fmp_empty": 50,
 "note": "FMP 无覆盖票不计入分母;n=0 时以 MFE 单源为准",
 "disagreements": [],
 "elapsed_s": 3.1
}
```

## 审计项 5 ✅
```json
{
 "pass": true,
 "note": "本 markdown 即产物",
 "elapsed_s": 0.0
}
```

## 审计项 6 ❌
```json
{
 "pass": false,
 "error": "cannot reindex on an axis with duplicate labels",
 "elapsed_s": 1.3
}
```

## 审计项 7 ✅
```json
{
 "pass": true,
 "dist": {},
 "missing_rate": 0.0,
 "elapsed_s": 12.4
}
```

## 审计项 8 ⏸
```json
{
 "pass": null,
 "note": "社媒情绪覆盖审计随文本线推迟(§1.1)",
 "elapsed_s": 0.0
}
```

## 审计项 9 ✅
```json
{
 "pass": true,
 "dup_multiplicity_dist": {
  "60": 179,
  "1": 20
 },
 "dup_frac": 0.8995,
 "rule": "(symbol,date) keep latest create_time(已在 annual_statement 实现)",
 "elapsed_s": 0.4
}
```

## 审计项 10 ✅
```json
{
 "pass": true,
 "rows": [
  {
   "vintage": 2019,
   "n": 3000,
   "mongo_coverage": 0.5773
  },
  {
   "vintage": 2020,
   "n": 3000,
   "mongo_coverage": 0.6143
  },
  {
   "vintage": 2021,
   "n": 3000,
   "mongo_coverage": 0.6007
  },
  {
   "vintage": 2022,
   "n": 3000,
   "mongo_coverage": 0.668
  },
  {
   "vintage": 2023,
   "n": 3000,
   "mongo_coverage": 0.7327
  },
  {
   "vintage": 2024,
   "n": 3000,
   "mongo_coverage": 0.777
  },
  {
   "vintage": 2025,
   "n": 3000,
   "mongo_coverage": 0.7453
  }
 ],
 "handling": "报表主源=Polygon financials(含退市);SUE=幸存者掩码双口径(§6.8)",
 "elapsed_s": 0.5
}
```

## 审计项 11 ✅
```json
{
 "pass": true,
 "ledger_aiss": {
  "exists": true,
  "n": 27,
  "first_date": "2026-06-02"
 },
 "ledger_ssrs": {
  "exists": true,
  "n": 20,
  "first_date": "2026-06-01"
 },
 "combined_signals": {
  "n_files": 87,
  "first": "combined_signals_20260319_124818.json",
  "last": "combined_signals_20260725_083044.json"
 },
 "inv_mrpt": {
  "exists": true
 },
 "inv_mtfs": {
  "exists": true
 },
 "inv_aiss": {
  "exists": true
 },
 "inv_ssrs": {
  "exists": true
 },
 "acct_aiss": {
  "exists": true
 },
 "acct_ssrs": {
  "exists": true
 },
 "elapsed_s": 0.0
}
```

## 审计项 12 ✅
```json
{
 "pass": true,
 "spdr_in_config": [
  "SMH",
  "SOXX",
  "SPY",
  "XLB",
  "XLC",
  "XLE",
  "XLF",
  "XLI",
  "XLK",
  "XLP",
  "XLRE",
  "XLU",
  "XLV",
  "XLY"
 ],
 "ssrs_actual": [
  "XLB",
  "XLC",
  "XLE",
  "XLF",
  "XLI",
  "XLK",
  "XLP",
  "XLRE",
  "XLU",
  "XLV",
  "XLY"
 ],
 "missing_from_config": [],
 "aiss_subsector_map": "待 P0 快照(config.aiss_subsectors)",
 "elapsed_s": 0.0
}
```
