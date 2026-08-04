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
  note TEXT);

CREATE TABLE IF NOT EXISTS fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id INTEGER NOT NULL, ts_utc TEXT NOT NULL, ticker TEXT NOT NULL,
  side TEXT NOT NULL, price REAL NOT NULL, count INTEGER NOT NULL,
  fee_usd REAL NOT NULL, mode TEXT NOT NULL DEFAULT 'paper');

CREATE TABLE IF NOT EXISTS marks(
  ts TEXT NOT NULL, decision_id INTEGER NOT NULL, ticker TEXT NOT NULL,
  mid REAL, pnl_usd REAL, PRIMARY KEY(ts, decision_id, ticker));

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
