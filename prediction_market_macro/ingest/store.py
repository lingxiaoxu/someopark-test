"""ingest/store.py — sqlite schema + connection discipline (PLAN §5, §5-bis).

Single-file db. Every data row carries the two-clock pair where relevant:
`event_time` (what the datum describes) and `knowledge_time` (earliest instant we could
have known it) — ALL PIT filtering is on knowledge_time (UTC ISO strings throughout).

Concurrency (PLAN §5): WAL + busy_timeout=30s; watchdog writes only runs/alerts.
Nothing is ever DELETEd; supersession is by insertion (INSERT OR REPLACE on natural keys).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS fred_obs(
  sid TEXT NOT NULL, event_time TEXT NOT NULL, value REAL,
  vintage_date TEXT NOT NULL,            -- ALFRED realtime_start (date granularity)
  knowledge_time TEXT NOT NULL,          -- vintage_date + official release time (UTC)
  first_seen_ts TEXT NOT NULL,
  PRIMARY KEY(sid, event_time, vintage_date));

CREATE TABLE IF NOT EXISTS fut_daily(
  root TEXT NOT NULL, event_time TEXT NOT NULL,   -- bar date
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  knowledge_time TEXT NOT NULL,          -- bar close instant UTC (completed bars only)
  first_seen_ts TEXT NOT NULL,
  PRIMARY KEY(root, event_time));

CREATE TABLE IF NOT EXISTS fx_daily(
  pair TEXT NOT NULL, event_time TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  knowledge_time TEXT NOT NULL, first_seen_ts TEXT NOT NULL,
  PRIMARY KEY(pair, event_time));

CREATE TABLE IF NOT EXISTS news(
  id TEXT PRIMARY KEY, published_utc TEXT NOT NULL, title TEXT, publisher TEXT,
  tickers TEXT, url TEXT, first_seen_ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS quotes(
  ts TEXT NOT NULL, ticker TEXT NOT NULL,
  yes_bid REAL, yes_ask REAL, bid_depth REAL, ask_depth REAL,
  PRIMARY KEY(ts, ticker));

CREATE TABLE IF NOT EXISTS contracts(
  ticker TEXT PRIMARY KEY, series TEXT NOT NULL, event_ticker TEXT NOT NULL,
  period TEXT NOT NULL, sub_title TEXT, strike_type TEXT, floor_strike REAL, cap_strike REAL,
  close_time TEXT, status TEXT, first_seen_ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS settlements(
  ticker TEXT PRIMARY KEY, series TEXT, period TEXT, result TEXT,
  settled_ts TEXT, first_seen_ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS releases(               -- calendars: scheduled vs actual (§5-bis.4)
  cal TEXT NOT NULL, period TEXT NOT NULL,
  scheduled_ts TEXT NOT NULL, actual_ts TEXT,
  PRIMARY KEY(cal, period));

CREATE TABLE IF NOT EXISTS features(
  series TEXT NOT NULL, asof TEXT NOT NULL, name TEXT NOT NULL,
  value REAL, source TEXT, knowledge_time TEXT NOT NULL,
  PRIMARY KEY(series, asof, name));

CREATE TABLE IF NOT EXISTS preds(
  series TEXT NOT NULL, period TEXT NOT NULL, asof TEXT NOT NULL,
  model_version TEXT NOT NULL, dist_json TEXT NOT NULL, ladder_json TEXT,
  inputs_json TEXT,                      -- Pred.inputs evidence chain (依据链)
  data_horizon TEXT NOT NULL,            -- max knowledge_time used; asserted <= asof on write
  created_ts TEXT NOT NULL,
  PRIMARY KEY(series, period, asof, model_version));

CREATE TABLE IF NOT EXISTS models(
  series TEXT NOT NULL, version TEXT NOT NULL, params_json TEXT NOT NULL,
  trained_through TEXT NOT NULL, created_ts TEXT NOT NULL, card_md TEXT,
  PRIMARY KEY(series, version));

CREATE TABLE IF NOT EXISTS param_selection(       -- #119 daily DSR-gated param choice
  -- One row per (series, day). `params_json` is '{}' when the gate held and the
  -- registered defaults were used, which is the common case and must stay
  -- distinguishable from "the selector did not run" (no row at all). `report_json`
  -- carries dsr.select's verdict verbatim so a later reader can see WHY, not just what.
  series TEXT NOT NULL, day TEXT NOT NULL,
  params_json TEXT NOT NULL, adopted INTEGER NOT NULL,
  n_obs INTEGER, n_trials INTEGER, dsr_p REAL,
  report_json TEXT NOT NULL, created_ts TEXT NOT NULL,
  PRIMARY KEY(series, day));

CREATE TABLE IF NOT EXISTS param_selection_cache(  -- #202 selector answers by SAMPLE
  -- `param_selection` above is keyed by DAY, because production asks the selector one
  -- question a day. #199's walk-forward gate history asks it at every graded event's
  -- own asof, which lands on days production never stood on — so the day-keyed
  -- carry-forward cannot serve them and every one is a full grid rescore (~9s each,
  -- measured; 105 minutes added to a 40-second 30d run).
  --
  -- The key here is `param_select._sample_key`: the sample fingerprint plus the manual
  -- override stamp. It is the module's OWN invalidation contract — two `before`
  -- instants carrying the same key see the same events and the same override, so
  -- `select_for` cannot tell them apart. That is the assumption `refresh`'s
  -- carry-forward already runs on; this table only widens its scope from one day to
  -- any day.
  --
  -- Like the fingerprint, this is deliberately NOT invalidated by a code change.
  -- `refresh(force=True)` clears it, which is the same remedy the module docstring
  -- already names for the day-keyed cache.
  series TEXT NOT NULL, sample_key TEXT NOT NULL,
  params_json TEXT NOT NULL, adopted INTEGER NOT NULL,
  report_json TEXT, created_ts TEXT NOT NULL,
  PRIMARY KEY(series, sample_key));

CREATE TABLE IF NOT EXISTS experiments(
  name TEXT NOT NULL, config_hash TEXT NOT NULL, series TEXT, window TEXT,
  metrics_json TEXT, created_ts TEXT NOT NULL,
  PRIMARY KEY(name, config_hash));

CREATE TABLE IF NOT EXISTS runs(                   -- §8.2 never-miss machinery
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lane TEXT NOT NULL, series TEXT NOT NULL, period TEXT NOT NULL, task TEXT NOT NULL,
  due_ts TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'due',   -- due|done|late|MISSED
  done_ts TEXT, note TEXT,
  UNIQUE(lane, series, period, task, due_ts));

CREATE TABLE IF NOT EXISTS coverage(               -- §8.1 lifecycle state machine
  series TEXT NOT NULL, period TEXT NOT NULL,
  state TEXT NOT NULL,        -- scheduled|armed|snapshotting|predicted|decided|passed|frozen|settled|reconciled|postponed
  updated_ts TEXT NOT NULL,
  PRIMARY KEY(series, period));

CREATE TABLE IF NOT EXISTS decisions(              -- §12 append-only PIT ledger (no UPDATE ever)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL, series TEXT NOT NULL, period TEXT NOT NULL,
  structure_json TEXT NOT NULL,          -- legs: [{ticker, side, price, count}]
  kind TEXT NOT NULL,                    -- open|exit|cancel|pass
  fair REAL, ask REAL, net_edge REAL, size_usd REAL,
  inputs_json TEXT NOT NULL, model_version TEXT NOT NULL, gate_snapshot TEXT NOT NULL,
  note TEXT,
  -- #149. On a close row (exit|cancel|settle_note), the id of the open row it retires.
  -- NULL on open rows, and NULL on every close written before this column existed.
  -- Without it a close is only attributable to a (series, period), not to a POSITION,
  -- and the four "do I already hold this?" checks each counted one stream's opens
  -- against every stream's closes.
  closes_decision_id INTEGER);

CREATE TABLE IF NOT EXISTS fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id INTEGER NOT NULL, ts_utc TEXT NOT NULL, ticker TEXT NOT NULL,
  side TEXT NOT NULL, price REAL NOT NULL, count INTEGER NOT NULL,
  fee_usd REAL NOT NULL, mode TEXT NOT NULL DEFAULT 'paper');

CREATE TABLE IF NOT EXISTS marks(
  ts TEXT NOT NULL, decision_id INTEGER NOT NULL, ticker TEXT NOT NULL,
  mid REAL, pnl_usd REAL, PRIMARY KEY(ts, decision_id, ticker));

CREATE TABLE IF NOT EXISTS shadow_exits(           -- PR-7 step 1 (#143): recorded, NEVER executed
  ts_utc TEXT NOT NULL, rule TEXT NOT NULL,        -- 'S2' = hold_edge <= 0
  decision_id INTEGER NOT NULL,                    -- the OPEN row this shadows
  series TEXT NOT NULL, period TEXT NOT NULL,
  hold_edge REAL, triggered INTEGER NOT NULL,      -- 1 = the rule would have closed here
  realized_usd REAL,                               -- what closing NOW would have realized
  legs_json TEXT NOT NULL,                         -- per-leg transactable exit price + depth
  note TEXT,
  PRIMARY KEY(ts_utc, rule, decision_id));

CREATE TABLE IF NOT EXISTS shadow_argmax(          -- PR-2 (#126): the defer-to-market arm
  ts_utc TEXT NOT NULL,
  series TEXT NOT NULL, period TEXT NOT NULL,
  arm TEXT NOT NULL,                               -- 'placed' = filter let it through
                                                   -- 'deferred' = filter killed it
  fair REAL NOT NULL, cost REAL NOT NULL,          -- deferred iff fair > cost
  count INTEGER NOT NULL, size_usd REAL NOT NULL,
  desc TEXT, legs_json TEXT NOT NULL,              -- fill prices, so PnL needs no replay
  note TEXT,
  PRIMARY KEY(series, period));                    -- one argmax leg per event, both arms

CREATE TABLE IF NOT EXISTS shadow_series_enable(  -- §25.4 (#155): recorded, NEVER acted on
  day TEXT NOT NULL, series TEXT NOT NULL,
  evaluated INTEGER NOT NULL,                      -- 0 = no stored verdict (gate is blind)
  would_block INTEGER NOT NULL,                    -- 1 = it would switch the series off
  roi REAL, n INTEGER, flips INTEGER,              -- the trailing window it decided on
  reason TEXT,                                     -- the exact ledger-shaped veto string
  ts_utc TEXT NOT NULL,
  PRIMARY KEY(day, series));                       -- one verdict per series per day

CREATE TABLE IF NOT EXISTS llm_annotations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL,
  input_hash TEXT NOT NULL, output_json TEXT);

CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL,
  acked INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS candles(
  ticker TEXT NOT NULL, end_ts INTEGER NOT NULL,
  yes_bid_close REAL, yes_ask_close REAL, price_close REAL, volume REAL,
  PRIMARY KEY(ticker, end_ts));

CREATE TABLE IF NOT EXISTS nowcast_vintages(       -- GDPNow / Cleveland Fed (§5-bis.1 口4)
  source TEXT NOT NULL, target TEXT NOT NULL, event_time TEXT NOT NULL,
  value REAL, knowledge_time TEXT NOT NULL, first_seen_ts TEXT NOT NULL,
  PRIMARY KEY(source, target, knowledge_time));

CREATE TABLE IF NOT EXISTS source_scores(          -- per-event per-source OOS Brier
  series TEXT NOT NULL, period TEXT NOT NULL, source TEXT NOT NULL,  -- model|market|bridge
  offset TEXT NOT NULL,                            -- '-1h' | '-24h'
  brier REAL, n_legs INTEGER, created_ts TEXT NOT NULL,
  PRIMARY KEY(series, period, source, offset));    -- ensemble weight learning (§19-2)

CREATE TABLE IF NOT EXISTS fed_statements(         -- FOMC statement text (§7 fed, §10)
  period TEXT NOT NULL,                            -- meeting period '2026-07'
  release_date TEXT NOT NULL,                      -- statement date; a month can hold two
                                                   -- (2008-01: 22nd intermeeting + 30th)
  url TEXT NOT NULL, text TEXT NOT NULL,
  knowledge_time TEXT NOT NULL,                    -- release instant UTC — the PIT key
  time_source TEXT NOT NULL,                       -- 'page' | 'eod' (see fed_text)
  fetched_ts TEXT NOT NULL,
  PRIMARY KEY(period, release_date));

CREATE TABLE IF NOT EXISTS event_flags(            -- LLM structural-break layer (§19-8)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, tag TEXT NOT NULL,             -- mass_layoffs|energy_shock|...
  direction TEXT, family TEXT NOT NULL,            -- affected series family
  severity INTEGER NOT NULL DEFAULT 1,             -- 1..5
  expires_ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS demo_orders(            -- §30 mirror: one row per paper fill
  fill_id INTEGER PRIMARY KEY,                     -- the paper fills.id being mirrored, 1:1
  decision_id INTEGER NOT NULL,
  client_order_id TEXT NOT NULL UNIQUE,            -- 'spm-m{fill_id}', deterministic
  ticker TEXT NOT NULL, side TEXT NOT NULL,        -- side: yes|no (original, not close_*)
  action TEXT NOT NULL,                            -- buy|sell
  count_target INTEGER NOT NULL,                   -- MIRROR_MULT x paper count
  count_filled INTEGER NOT NULL DEFAULT 0,
  paper_ask REAL NOT NULL,                         -- paper fill price (diff baseline, ¢/张)
  prod_ask_at_send REAL,                           -- prod book re-fetched at send (latency)
  avg_price REAL, fee_usd REAL,                    -- demo actuals (venue component)
  status TEXT NOT NULL,                            -- dryrun|intent|sent|filled|partial|
                                                   -- unfilled|skipped_halt|skipped_power|
                                                   -- skipped_gate|skipped_noheld
  order_id TEXT, ts_sent TEXT, ts_terminal TEXT, note TEXT);

CREATE TABLE IF NOT EXISTS demo_fills(             -- §30 actual demo executions (poll results)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fill_id INTEGER NOT NULL,                        -- back-link demo_orders.fill_id
  ticker TEXT NOT NULL, side TEXT NOT NULL, action TEXT NOT NULL,
  price REAL NOT NULL, count INTEGER NOT NULL, fee_usd REAL NOT NULL,
  exchange_fill_id TEXT UNIQUE, ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS demo_balance_sheet(     -- §30.4 append-only snapshots
  ts TEXT PRIMARY KEY,
  cash_exchange REAL NOT NULL,                     -- GET /portfolio/balance, live
  cash_expected REAL NOT NULL,                     -- local derivation (identity RHS)
  reserved_usd REAL NOT NULL,                      -- in-flight (non-terminal) order hold
  positions_cost REAL NOT NULL,
  positions_mtm REAL NOT NULL,                     -- marked on the PRODUCTION book
  equity REAL NOT NULL,                            -- cash_exchange + positions_mtm
  realized_cum REAL NOT NULL, fees_cum REAL NOT NULL,
  n_open_positions INTEGER NOT NULL,
  exposure_json TEXT NOT NULL,                     -- per-series exposure vs risk caps
  drift_usd REAL NOT NULL);                        -- |cash_exchange - cash_expected|

CREATE TABLE IF NOT EXISTS demo_mirror_state(      -- §30 k/v: armed | halt | watermark
  k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS demo_transfers(         -- §30.4 deposits/withdrawals ledger
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  amount_usd REAL NOT NULL,                        -- always positive; kind carries the sign
  kind TEXT NOT NULL CHECK(kind IN ('deposit','withdrawal')),
  note TEXT NOT NULL,                              -- mandatory: who/why, never ''
  recorded_ts TEXT NOT NULL);                      -- when the row was written (vs ts = when
                                                   -- the transfer happened at the exchange)

-- ── DFM synthetic sample (PLAN_DFM_SYNTH S7) ────────────────────────────────
-- The consumable output only. The worlds themselves are multi-GB sqlite files under
-- data/synth/ and are regenerable; what the daily lane needs is one number per candidate
-- parameter set, which is small enough to live here and be caught by ops/backup_db.py.
CREATE TABLE IF NOT EXISTS synth_runs(             -- one generation of synthetic worlds
  run_id TEXT PRIMARY KEY,                         -- series:cutoff:seed
  series TEXT NOT NULL,
  cutoff_ts TEXT NOT NULL,                         -- generator training cut (= now - 75d)
  splice_ts TEXT NOT NULL,                         -- first instant that is invented
  built_ts TEXT NOT NULL,                          -- staleness is measured against this
  n_paths INTEGER NOT NULL, n_events INTEGER NOT NULL,
  grid_hash TEXT NOT NULL,                         -- the grid these scores are FOR
  meta_json TEXT NOT NULL);                        -- panel, generator, coverage, clocks
CREATE INDEX IF NOT EXISTS ix_synth_runs_series ON synth_runs(series, built_ts);

CREATE TABLE IF NOT EXISTS synth_scores(           -- per candidate, mean PnL per event
  run_id TEXT NOT NULL, set_hash TEXT NOT NULL,
  set_json TEXT NOT NULL, set_idx INTEGER NOT NULL,
  n_events INTEGER NOT NULL, mean_pnl REAL NOT NULL, sd_pnl REAL NOT NULL,
  PRIMARY KEY(run_id, set_hash));

CREATE TABLE IF NOT EXISTS synth_wf_mats(          -- S5-WF: per-release scored matrices
  series TEXT NOT NULL, release_tok TEXT NOT NULL, -- one row per settled monthly release
  cutoff_ts TEXT NOT NULL,                         -- generator splice = close - 75d (PIT)
  built_ts TEXT NOT NULL,
  grid_json TEXT NOT NULL,                         -- ordered candidate sets (default first)
  grid_hash TEXT NOT NULL,
  real_json TEXT NOT NULL,                         -- rows x K real PnL ([] if unscoreable)
  synth_json TEXT NOT NULL,                        -- rows x K synthetic PnL
  meta_json TEXT NOT NULL,
  PRIMARY KEY(series, release_tok));               -- worlds are deleted after scoring;
                                                   -- the matrices ARE the measurement

CREATE TABLE IF NOT EXISTS synth_lambda(           -- what one synthetic event is worth
  series TEXT NOT NULL,                            -- '*' is the pooled value the lane uses
  measured_ts TEXT NOT NULL,
  lam REAL NOT NULL, lam_point REAL NOT NULL, lam_lo REAL NOT NULL, lam_hi REAL NOT NULL,
  rho REAL, n_real INTEGER, n_synth INTEGER, k INTEGER,
  detail_json TEXT NOT NULL,
  PRIMARY KEY(series, measured_ts));
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    # lightweight migrations (idempotent)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(preds)")}
    if "inputs_json" not in cols:
        conn.execute("ALTER TABLE preds ADD COLUMN inputs_json TEXT")
    # #149. `decisions` is CREATE TABLE IF NOT EXISTS, so the DDL above only reaches a
    # FRESH db — the live ledger needs the ALTER or `closes_decision_id` exists in tests
    # and nowhere else, which is the worst of both.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    if "closes_decision_id" not in cols:
        conn.execute("ALTER TABLE decisions ADD COLUMN closes_decision_id INTEGER")
    # §30.4 transfers ledger: the balance identity gained a deposits/withdrawals term, and
    # the snapshot row records its cumulative value so every historical drift stays
    # interpretable. Same IF-NOT-EXISTS trap as #149 above — the DDL only reaches a fresh
    # db, the live one needs the ALTER.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(demo_balance_sheet)")}
    if "transfers_cum" not in cols:
        conn.execute("ALTER TABLE demo_balance_sheet ADD COLUMN transfers_cum REAL"
                     " NOT NULL DEFAULT 0.0")
    _migrate_fed_statements(conn)
    conn.commit()
    return conn


def _migrate_fed_statements(conn) -> None:
    """v1 (period PK, fetched_ts only) → v2 (release_date in the key, knowledge_time).

    Two reasons the old shape had to go: it carried NO time column at all, so the table
    could not be PIT-filtered (PLAN §5-bis), and `period` as the sole key silently drops
    the second statement of a month — 2008-01 (22nd intermeeting + 30th scheduled) and
    2020-03 (3rd + 15th) are exactly the meetings a Fed model most wants to read.

    Old rows are carried over, not dropped: release_date comes from the URL's YYYYMMDD
    and the time is stamped conservatively (see fed_text._release_ts). The backfill
    re-fetches them with the parsed page time and REPLACEs on the same key.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fed_statements)")}
    if not cols or "release_date" in cols:
        return
    import re as _re

    from prediction_market_macro.ingest.fed_text import eod_knowledge_time
    rows = conn.execute("SELECT period, url, text, fetched_ts FROM fed_statements").fetchall()
    conn.execute("ALTER TABLE fed_statements RENAME TO fed_statements_v1")
    conn.executescript(SCHEMA)
    for r in rows:
        m = _re.search(r"(\d{8})", r["url"] or "")
        if not m:
            continue                      # unparseable URL: left in fed_statements_v1
        d = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
        conn.execute(
            "INSERT OR REPLACE INTO fed_statements(period, release_date, url, text,"
            " knowledge_time, time_source, fetched_ts) VALUES(?,?,?,?,?,'eod',?)",
            (r["period"], d, r["url"], r["text"], eod_knowledge_time(d), r["fetched_ts"]))
