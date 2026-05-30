# AISS — Removed SSRS-inherited files

These files were copied from `sector_rotation` (SSRS) but are **not used by the
AISS V1 strategy**. They were verified to have no importers in AISS production
code (only tests referenced some), documented here, then deleted to keep the
AISS package clean and self-consistent.

| Deleted file | Why it was SSRS-only / unused in AISS V1 | AISS replacement |
|---|---|---|
| `update_eps_history.py` | Maintained `eps_history.json` for SSRS's P/E value signal. AISS V1 has **no value factor**, so no EPS data is needed. (Also wrote to `price_data/sector_etfs/`, which AISS must not touch.) | none (AISS uses `cs_momentum + supply_chain + capex_pulse + cycle_regime`) |
| `signals/value.py` | P/E-percentile relative-value signal (SSRS factor 3). Not part of the AISS 4-factor composite. No AISS production importer. | `signals/supply_chain.py` (the AISS core factor) |
| `signals/new_signals.py` | SSRS "V2" extra factors (short-term mom, earnings-revision, RS-breakout, low-vol). AISS V1 composite does not use them; `composite.py` was rewritten without them. | folded into AISS `cs_momentum` / `supply_chain` / `capex_pulse` design |
| `portfolio/dual_sleeve.py` | A core/tactical dual-sleeve constructor that was **never wired into the engine** even in SSRS (orphan module). | AISS uses single-sleeve `optimizer.optimize_weights` |

## Notes
- `signals/risk_overlay.py` is **kept** (referenced by the engine's optional v2
  path; harmless, available if a v2 signal version is later enabled).
- `smart_select.py`, `multi_horizon_backtest.py`, `weekly_review.py`,
  `walk_forward.py` are **kept and renamed to AISS** (production param-selection
  / review framework, mirroring SSRS).
- Tests that exercised the deleted factors (`tests/test_signals.py` value cases,
  `tests/test_new_features.py`) were trimmed to AISS factors.

Deleted on 2026-05-29 during the AISS build.
