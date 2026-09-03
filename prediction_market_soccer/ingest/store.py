"""Local persistence for soccer data (plan 02 §3, 05 §2).

A self-contained, zero-infra store: **SQLite** for idempotent business tables +
**append-only JSON raw snapshots** on disk for replay (plan 02 §3.4). This keeps
the project isolated (no Postgres server) while giving ACID upserts, watermarks
for incremental sync, and full request accounting against the monthly budget.

All writes are idempotent (upsert on natural keys). Re-running a sync never
duplicates rows; raw snapshots are the only append-only layer (one file per API
response, timestamped) so any downstream bug can be recomputed from history —
backtest and live share the same path (plan 02 §3.4).

Time is stored as UTC ISO-8601 strings. Migrating to Postgres later only means
swapping this module's connection + dialect.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from prediction_market_soccer.config import CONFIG

DB_PATH = CONFIG.paths.data / "soccer.db"
RAW_DIR = CONFIG.paths.raw_snapshots


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def params_hash(params: dict | None) -> str:
    blob = json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ── Schema ───────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS team (
    api_id INTEGER PRIMARY KEY, name TEXT, code TEXT, country TEXT,
    founded INTEGER, national INTEGER, logo TEXT, raw_json TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS team_meta (
    api_id INTEGER PRIMARY KEY, group_code TEXT, fifa_rank INTEGER,
    canonical_team_id TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS player (
    api_id INTEGER PRIMARY KEY, name TEXT, firstname TEXT, lastname TEXT,
    age INTEGER, nationality TEXT, position TEXT, photo TEXT,
    raw_json TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS squad (
    team_api_id INTEGER, player_api_id INTEGER, season INTEGER,
    position TEXT, number INTEGER, updated_at TEXT,
    PRIMARY KEY (team_api_id, player_api_id, season)
);
CREATE TABLE IF NOT EXISTS player_stat (
    player_api_id INTEGER, league_id INTEGER, season INTEGER, team_api_id INTEGER,
    appearances INTEGER, minutes INTEGER, goals INTEGER, assists INTEGER,
    shots_total INTEGER, shots_on INTEGER, penalty_scored INTEGER, rating REAL,
    raw_json TEXT, updated_at TEXT,
    PRIMARY KEY (player_api_id, league_id, season)
);
CREATE TABLE IF NOT EXISTS fixture (
    api_id INTEGER PRIMARY KEY, league_id INTEGER, season INTEGER, round TEXT,
    kickoff_ts TEXT, status_short TEXT, status_long TEXT, elapsed INTEGER,
    home_api_id INTEGER, away_api_id INTEGER, home_goals INTEGER, away_goals INTEGER,
    venue_name TEXT, venue_city TEXT, raw_json TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fixture_event (
    fixture_api_id INTEGER, seq INTEGER, minute INTEGER, extra INTEGER,
    team_api_id INTEGER, player_api_id INTEGER, assist_api_id INTEGER,
    type TEXT, detail TEXT, comments TEXT, raw_json TEXT,
    PRIMARY KEY (fixture_api_id, seq)
);
CREATE TABLE IF NOT EXISTS fixture_stats (
    fixture_api_id INTEGER, team_api_id INTEGER, xg REAL, goals_prevented REAL,
    shots_total INTEGER, shots_on INTEGER, possession REAL, corners INTEGER,
    raw_json TEXT, fetched_at TEXT,
    PRIMARY KEY (fixture_api_id, team_api_id)
);
CREATE TABLE IF NOT EXISTS price_tick (
    -- per-minute (or finer) market price path per outcome, from Polymarket Global
    -- prices-history. `rel_min` = minutes since kickoff (negative = pre-match). Enables
    -- fine-grained exit-timing / cash-out research beyond the 7 milestone checkpoints.
    fixture_api_id INTEGER, side TEXT, ts INTEGER, rel_min INTEGER, price REAL,
    venue TEXT DEFAULT 'poly_global',
    PRIMARY KEY (fixture_api_id, side, ts)
);
CREATE TABLE IF NOT EXISTS price_tick_adv (
    -- 2-way "advance" market per-minute price path (plan 24 §7). SEPARATE from price_tick
    -- so side='home'/'away' never collides with the 3-way home/away ticks at the same ts.
    -- Feeds smart_exit_advance + the advance price-track. `rel_min` = minutes since kickoff.
    fixture_api_id INTEGER, side TEXT, ts INTEGER, rel_min INTEGER, price REAL,
    venue TEXT DEFAULT 'poly_global',
    PRIMARY KEY (fixture_api_id, side, ts)
);
CREATE TABLE IF NOT EXISTS lineup (
    fixture_api_id INTEGER, team_api_id INTEGER, formation TEXT, coach TEXT,
    raw_json TEXT, fetched_at TEXT,
    PRIMARY KEY (fixture_api_id, team_api_id)
);
CREATE TABLE IF NOT EXISTS fixture_player_stats (
    fixture_api_id INTEGER, team_api_id INTEGER, player_api_id INTEGER,
    player_name TEXT, position TEXT, is_starter INTEGER, captain INTEGER,
    minutes INTEGER, rating REAL,
    shots_total INTEGER, shots_on INTEGER, goals INTEGER, assists INTEGER,
    passes_total INTEGER, passes_key INTEGER, pass_accuracy INTEGER,
    dribbles_attempts INTEGER, dribbles_success INTEGER,
    duels_total INTEGER, duels_won INTEGER,
    tackles INTEGER, interceptions INTEGER,
    fouls_drawn INTEGER, fouls_committed INTEGER, offsides INTEGER,
    raw_json TEXT, fetched_at TEXT,
    PRIMARY KEY (fixture_api_id, player_api_id)
);
CREATE TABLE IF NOT EXISTS injury (
    fixture_api_id INTEGER, team_api_id INTEGER, player_api_id INTEGER,
    type TEXT, reason TEXT, raw_json TEXT, fetched_at TEXT,
    PRIMARY KEY (fixture_api_id, player_api_id)
);
CREATE TABLE IF NOT EXISTS standing (
    league_id INTEGER, season INTEGER, group_code TEXT, team_api_id INTEGER,
    rank INTEGER, points INTEGER, goals_diff INTEGER, played INTEGER,
    win INTEGER, draw INTEGER, lose INTEGER, form TEXT, raw_json TEXT, updated_at TEXT,
    PRIMARY KEY (league_id, season, team_api_id)
);
CREATE TABLE IF NOT EXISTS nt_recent (
    fixture_api_id INTEGER, team_api_id INTEGER, opp_api_id INTEGER,
    kickoff_ts TEXT, league_id INTEGER, is_friendly INTEGER,
    gf INTEGER, ga INTEGER, is_home INTEGER, fetched_at TEXT,
    PRIMARY KEY (fixture_api_id, team_api_id)
);
CREATE TABLE IF NOT EXISTS fc_player (
    fc_id INTEGER PRIMARY KEY, name TEXT, nationality TEXT, canonical_team_id TEXT,
    position TEXT, position_type TEXT, overall INTEGER, finishing INTEGER,
    positioning INTEGER, shot_power INTEGER, penalties INTEGER, sho INTEGER,
    pen_taker INTEGER, goal_rate REAL, team_attack_rank INTEGER, source TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS h2h (
    pair_key TEXT, fixture_api_id INTEGER, kickoff_ts TEXT,
    home_api_id INTEGER, away_api_id INTEGER, home_goals INTEGER, away_goals INTEGER,
    raw_json TEXT, updated_at TEXT,
    PRIMARY KEY (pair_key, fixture_api_id)
);
CREATE TABLE IF NOT EXISTS prediction (
    fixture_api_id INTEGER PRIMARY KEY, p_home REAL, p_draw REAL, p_away REAL,
    advice TEXT, winner_name TEXT, raw_json TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS match_odds (
    fixture_api_id INTEGER, bookmaker TEXT, p_home REAL, p_draw REAL, p_away REAL,
    overround REAL, raw_json TEXT, fetched_at TEXT,
    PRIMARY KEY (fixture_api_id, bookmaker)
);
CREATE TABLE IF NOT EXISTS watermark (
    resource TEXT PRIMARY KEY, last_synced_at TEXT, params_hash TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS api_call (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, endpoint TEXT, params_json TEXT,
    http_status INTEGER, results_count INTEGER, req_remaining INTEGER, req_limit INTEGER
);
CREATE TABLE IF NOT EXISTS raw_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT, resource TEXT, params_hash TEXT,
    fetched_at TEXT, path TEXT
);
-- ── venue / cross-venue / model / signal layer (plan 05 §2) ──────────────────
CREATE TABLE IF NOT EXISTS venue (
    venue_id TEXT PRIMARY KEY, name TEXT, base_url TEXT, exec_legal_flag INTEGER
);
CREATE TABLE IF NOT EXISTS xref (
    real_entity_id TEXT, real_entity_type TEXT, kalshi_ticker TEXT,
    pm_us_slug TEXT, pm_global_token_id TEXT, equiv_verified_flag INTEGER DEFAULT 0,
    equiv_notes TEXT, tie_rule_kalshi TEXT, tie_rule_poly TEXT, updated_at TEXT,
    PRIMARY KEY (real_entity_id, real_entity_type)
);
CREATE TABLE IF NOT EXISTS ob_snapshot (
    ts TEXT, venue_id TEXT, market_key TEXT,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL, depth_json TEXT,
    PRIMARY KEY (ts, venue_id, market_key)
);
CREATE TABLE IF NOT EXISTS xv_spread (
    ts TEXT, market_family TEXT, real_entity_id TEXT,
    p_model REAL, p_kalshi REAL, p_poly_us REAL, p_global REAL,
    xv_spread REAL, rel_value_kalshi REAL, verified_flag INTEGER DEFAULT 0,
    PRIMARY KEY (ts, market_family, real_entity_id)
);
CREATE TABLE IF NOT EXISTS model_run (
    run_ts TEXT PRIMARY KEY, n_sims INTEGER, code_version TEXT, data_watermark_json TEXT
);
CREATE TABLE IF NOT EXISTS sim_champion (
    run_ts TEXT, team_id TEXT, p_champion REAL, p_final REAL, p_sf REAL,
    p_qf REAL, p_r16 REAL, p_advance REAL, e_matches REAL,
    PRIMARY KEY (run_ts, team_id)
);
CREATE TABLE IF NOT EXISTS sim_golden_boot (
    run_ts TEXT, player_id TEXT, name TEXT, p_gboot REAL, e_goals REAL,
    PRIMARY KEY (run_ts, player_id)
);
CREATE TABLE IF NOT EXISTS signal (
    run_ts TEXT, venue_id TEXT, market_key TEXT, side TEXT, p_eff REAL, ask REAL,
    net_edge REAL, target_stake REAL, kind TEXT, action TEXT,
    PRIMARY KEY (run_ts, venue_id, market_key, side)
);
CREATE TABLE IF NOT EXISTS calibration (
    run_ts TEXT, metric TEXT, value REAL, n INTEGER, detail_json TEXT,
    PRIMARY KEY (run_ts, metric)
);
CREATE TABLE IF NOT EXISTS milestone_snapshot (
    fixture_api_id INTEGER, milestone TEXT, ts TEXT,
    elapsed INTEGER, status_short TEXT, home_goals INTEGER, away_goals INTEGER,
    p_model_home REAL, p_model_draw REAL, p_model_away REAL,
    kalshi_home_ask REAL, kalshi_home_bid REAL, kalshi_draw_ask REAL, kalshi_draw_bid REAL,
    kalshi_away_ask REAL, kalshi_away_bid REAL,
    poly_home_ask REAL, poly_home_bid REAL, poly_draw_ask REAL, poly_draw_bid REAL,
    poly_away_ask REAL, poly_away_bid REAL,
    devig_home REAL, devig_draw REAL, devig_away REAL,
    kalshi_adv_home_ask REAL, kalshi_adv_home_bid REAL, kalshi_adv_away_ask REAL, kalshi_adv_away_bid REAL,
    poly_adv_home_ask REAL, poly_adv_home_bid REAL, poly_adv_away_ask REAL, poly_adv_away_bid REAL,
    xg_home REAL, xg_away REAL, reds_home INTEGER, reds_away INTEGER,
    kalshi_ticker_home TEXT, kalshi_ticker_draw TEXT, kalshi_ticker_away TEXT,
    poly_token_home TEXT, poly_token_draw TEXT, poly_token_away TEXT,
    price_source TEXT,
    PRIMARY KEY (fixture_api_id, milestone)
);
-- Frozen bet ledger: each settled match's decision computed ONCE (point-in-time strength
-- + point-in-time calibration) and persisted, so the reported track record never mutates
-- as later matches settle. Append-only — history is never recomputed (ops/settle_bets.py).
CREATE TABLE IF NOT EXISTS settled_bet (
    fixture_api_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,                            -- JSON: frozen match_pick() output
    inplay_json TEXT,                                 -- JSON: frozen causal in-play entry (or NULL)
    cal_method TEXT, cal_param REAL, cal_n INTEGER,   -- the PIT calibration used (audit)
    settled_at TEXT NOT NULL                          -- when first frozen
);
-- ── venue order-book probe (ops/venue_liquidity.py) ─────────────────────────
-- One row per (fixture, side, venue, kickoff-relative bucket): what the book looked
-- like that far before kickoff. Exists because a single sample taken hours early was
-- used to conclude "this competition has no demo liquidity" — and the book filled up
-- minutes before kickoff, so the conclusion was wrong in both directions (it also
-- showed some competitions quoting 40 hours out). Now it is measured, per competition,
-- per offset, and never guessed.
CREATE TABLE IF NOT EXISTS venue_book_probe (
    fixture_api_id INTEGER NOT NULL,
    comp TEXT,
    venue TEXT NOT NULL,              -- 'demo' | 'prod'
    side TEXT NOT NULL,               -- home | draw | away
    bucket TEXT NOT NULL,             -- T-48h, T-24h, … T-3m (see ops/venue_liquidity.BUCKETS)
    minutes_to_kickoff REAL,
    ticker TEXT,
    bid REAL, ask REAL, bid_depth REAL, ask_depth REAL,
    ts TEXT NOT NULL,
    PRIMARY KEY (fixture_api_id, venue, side, bucket)
);
-- ── Kalshi DEMO mirror of the 择时(实现) strategy (exec/kalshi_mirror.py) ────────
-- One row per (fixture, track): the demo order that mirrors the paper ledger's
-- pre-match bet ('pre') or causal in-play entry ('inplay'), its fills, and its
-- smart-exit / settlement close-out. ledger_* columns record what the paper ledger
-- will freeze for the same bet (best-venue ask, $ stake) so the two can be reconciled.
CREATE TABLE IF NOT EXISTS kalshi_mirror (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_api_id INTEGER NOT NULL,
    track TEXT NOT NULL,                      -- 'pre' | 'inplay'
    comp TEXT,
    side TEXT NOT NULL,                       -- home | draw | away
    ticker TEXT NOT NULL,
    bet_kind TEXT,                            -- value | argmax (pre) | relative_value (inplay)
    entry_min INTEGER NOT NULL DEFAULT 0,     -- 0 = pre-match; milestone minute for in-play
    ledger_entry_c REAL, ledger_venue TEXT, ledger_stake_usd REAL, ledger_edge REAL,
    ask_c REAL,                               -- demo book ask at submission (what we paid up to)
    count INTEGER NOT NULL,                   -- contracts requested
    fill_count REAL NOT NULL DEFAULT 0,
    avg_fill_c REAL,
    order_id TEXT, client_order_id TEXT UNIQUE,
    status TEXT NOT NULL,                     -- pending|open|unfilled|exited|settled|error|skipped
    submitted_at TEXT, filled_at TEXT,
    exit_reason TEXT,                         -- smart_exit | settled
    exit_min INTEGER, exit_bid_c REAL, exit_fair_c REAL,
    exit_order_id TEXT, exit_client_order_id TEXT,
    exit_fill_count REAL NOT NULL DEFAULT 0, exit_avg_c REAL, exited_at TEXT,
    won INTEGER, pnl_c REAL,                  -- per-contract realised ¢ once closed
    attempts INTEGER NOT NULL DEFAULT 1,      -- venue-side failures are retried (≤3)
    note TEXT, raw_json TEXT,
    UNIQUE (fixture_api_id, track)
);
-- Every milestone_snapshot row is evaluated for a mirror entry exactly once.
CREATE TABLE IF NOT EXISTS kalshi_mirror_eval (
    fixture_api_id INTEGER NOT NULL,
    milestone TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    verdict TEXT,
    PRIMARY KEY (fixture_api_id, milestone)
);
-- ── club-soccer additions (TRANSFORM_PLAN §3.0/§3.6) ─────────────────────────
CREATE TABLE IF NOT EXISTS club_registry (
    club_id TEXT, comp TEXT, api_team_id INTEGER, name TEXT, zh TEXT,
    kalshi_code TEXT, kalshi_name TEXT, poly_code TEXT, logo TEXT,
    valid_from TEXT, valid_to TEXT, updated_at TEXT,
    PRIMARY KEY (club_id, comp)
);
CREATE TABLE IF NOT EXISTS tie (
    -- one two-legged knockout tie: pairs the two fixtures, carries the aggregate.
    tie_key TEXT PRIMARY KEY, comp TEXT, round TEXT,
    leg1_fixture_id INTEGER, leg2_fixture_id INTEGER,
    team_a_api_id INTEGER, team_b_api_id INTEGER,
    agg_a INTEGER, agg_b INTEGER, decided INTEGER DEFAULT 0, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_club_registry_comp ON club_registry (comp);
CREATE INDEX IF NOT EXISTS ix_tie_comp ON tie (comp);
CREATE INDEX IF NOT EXISTS ix_fixture_status ON fixture (status_short);
CREATE INDEX IF NOT EXISTS ix_fixture_kickoff ON fixture (kickoff_ts);
CREATE INDEX IF NOT EXISTS ix_api_call_ts ON api_call (ts);
CREATE INDEX IF NOT EXISTS ix_xv_spread_entity ON xv_spread (real_entity_id);
CREATE INDEX IF NOT EXISTS ix_signal_run ON signal (run_ts);
CREATE INDEX IF NOT EXISTS ix_fc_player_team ON fc_player (canonical_team_id);
"""

# Static venue registry (plan 05 §2): kalshi/poly_us executable, poly_global read-only.
_VENUE_SEED = [
    ("kalshi", "Kalshi", "https://external-api.kalshi.com/trade-api/v2", 1),
    ("poly_us", "Polymarket US", "https://api.polymarket.us", 1),
    ("poly_global", "Polymarket Global", "https://clob.polymarket.com", 0),
]


def connect() -> sqlite3.Connection:
    CONFIG.paths.ensure()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # durable + concurrent reads (frontend)
    # WAL permits ONE writer at a time; without a busy timeout the second writer gets an
    # immediate "database is locked" instead of queueing. That stayed invisible while a
    # single process owned all writes — the settle reports moving to a detached process
    # made it a real schedule: 11 lock errors in one afternoon, a Kalshi discovery that
    # failed to CONSTRUCT (its __init__ reads club_registry) so a live Serie A match
    # exported with no Kalshi quotes while the venue had the market open and active,
    # and one whole live cycle dying mid-upsert. Sixty seconds: the settle path used to
    # hold one write transaction across a whole freeze (now committed per fixture), and a
    # PIT prior rebuild inside the background report can still take tens of seconds.
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added to milestone_snapshot after its first release — ALTER-migrated onto
# existing DBs (CREATE TABLE IF NOT EXISTS won't add columns to a table that exists).
_MILESTONE_ADDED_COLS = [
    "kalshi_adv_home_ask", "kalshi_adv_home_bid", "kalshi_adv_away_ask", "kalshi_adv_away_bid",
    "poly_adv_home_ask", "poly_adv_home_bid", "poly_adv_away_ask", "poly_adv_away_bid",
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for tables that predate a new column (SQLite ALTER
    TABLE ADD COLUMN is cheap + safe; skipped when the column already exists)."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(milestone_snapshot)")}
    for col in _MILESTONE_ADDED_COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE milestone_snapshot ADD COLUMN {col} REAL")
    km = {r["name"] for r in conn.execute("PRAGMA table_info(kalshi_mirror)")}
    if km and "attempts" not in km:
        conn.execute("ALTER TABLE kalshi_mirror ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    conn = conn or connect()
    conn.executescript(_SCHEMA)
    _migrate(conn)
    for vid, name, url, flag in _VENUE_SEED:
        upsert(conn, "venue", {"venue_id": vid, "name": name, "base_url": url,
                               "exec_legal_flag": flag}, pk=["venue_id"])
    conn.commit()
    return conn


# ── model / signal / cross-venue persistence (plan 05 §2) ────────────────────
def persist_model_run(conn: sqlite3.Connection, run_ts: str, *, n_sims: int, code_version: str,
                      champion: list[dict], golden_boot: list[dict], watermark: dict | None = None) -> None:
    """Persist a model run + its per-team / per-player simulation outputs."""
    upsert(conn, "model_run", {
        "run_ts": run_ts, "n_sims": n_sims, "code_version": code_version,
        "data_watermark_json": json.dumps(watermark or {}, ensure_ascii=False),
    }, pk=["run_ts"])
    for c in champion:
        upsert(conn, "sim_champion", {
            "run_ts": run_ts, "team_id": c["team_id"], "p_champion": c["p_champion"],
            "p_final": c.get("p_final"), "p_sf": c.get("p_sf"), "p_qf": c.get("p_qf"),
            "p_r16": c.get("p_r16"), "p_advance": c.get("p_advance_model"),
            "e_matches": c.get("e_matches"),
        }, pk=["run_ts", "team_id"])
    for g in golden_boot:
        upsert(conn, "sim_golden_boot", {
            "run_ts": run_ts, "player_id": g["player_id"], "name": g.get("name"),
            "p_gboot": g["p_golden_boot"], "e_goals": g.get("e_goals"),
        }, pk=["run_ts", "player_id"])
    conn.commit()


def persist_xv_spread(conn: sqlite3.Connection, ts: str, family: str, rows: list[dict]) -> int:
    n = 0
    for r in rows:
        upsert(conn, "xv_spread", {
            "ts": ts, "market_family": family, "real_entity_id": r["team_id"],
            "p_model": r.get("p_model"), "p_kalshi": r.get("p_kalshi"),
            "p_poly_us": r.get("p_poly_us"), "p_global": r.get("p_global"),
            "xv_spread": r.get("xv_spread"), "rel_value_kalshi": r.get("rel_value_kalshi"),
            "verified_flag": 0,
        }, pk=["ts", "market_family", "real_entity_id"])
        n += 1
    conn.commit()
    return n


def persist_signals(conn: sqlite3.Connection, run_ts: str, signals: list[dict]) -> int:
    for s in signals:
        upsert(conn, "signal", {
            "run_ts": run_ts, "venue_id": s["venue_id"], "market_key": s["market_key"],
            "side": s["side"], "p_eff": s.get("p_eff"), "ask": s.get("ask"),
            "net_edge": s.get("net_edge"), "target_stake": s.get("target_stake"),
            "kind": s.get("kind"), "action": s.get("action"),
        }, pk=["run_ts", "venue_id", "market_key", "side"])
    conn.commit()
    return len(signals)


def persist_calibration(conn: sqlite3.Connection, run_ts: str, metrics: dict[str, tuple[float, int]]) -> None:
    for metric, (value, n) in metrics.items():
        upsert(conn, "calibration", {"run_ts": run_ts, "metric": metric, "value": value,
                                     "n": n, "detail_json": "{}"}, pk=["run_ts", "metric"])
    conn.commit()


# ── Idempotent upsert ────────────────────────────────────────────────────────
def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], pk: list[str]) -> None:
    """INSERT ... ON CONFLICT(pk) DO UPDATE — idempotent on natural keys."""
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])


def upsert_many(conn: sqlite3.Connection, table: str, rows: Iterable[dict], pk: list[str]) -> int:
    n = 0
    for r in rows:
        upsert(conn, table, r, pk)
        n += 1
    return n


# ── Watermarks (incremental gate) ────────────────────────────────────────────
def get_watermark(conn: sqlite3.Connection, resource: str) -> dict | None:
    cur = conn.execute("SELECT * FROM watermark WHERE resource=?", (resource,))
    row = cur.fetchone()
    return dict(row) if row else None


def set_watermark(conn: sqlite3.Connection, resource: str, *, params_hash: str = "", note: str = "") -> None:
    upsert(
        conn, "watermark",
        {"resource": resource, "last_synced_at": utcnow(), "params_hash": params_hash, "note": note},
        pk=["resource"],
    )


def is_fresh(conn: sqlite3.Connection, resource: str, ttl_seconds: int) -> bool:
    """True if ``resource`` was synced within ttl_seconds (skip → 0 API calls)."""
    wm = get_watermark(conn, resource)
    if not wm or not wm.get("last_synced_at"):
        return False
    last = datetime.fromisoformat(wm["last_synced_at"])
    return (datetime.now(timezone.utc) - last).total_seconds() < ttl_seconds


# ── Raw snapshots + API accounting ───────────────────────────────────────────
def write_raw_snapshot(conn: sqlite3.Connection, resource: str, params: dict | None, payload: Any) -> Path:
    ph = params_hash(params)
    ts = utcnow().replace(":", "").replace("-", "").split(".")[0]
    folder = RAW_DIR / resource
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ts}_{ph}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    conn.execute(
        "INSERT INTO raw_index (resource, params_hash, fetched_at, path) VALUES (?,?,?,?)",
        (resource, ph, utcnow(), str(path)),
    )
    return path


def log_api_call(conn: sqlite3.Connection, endpoint: str, params: dict | None, *,
                 http_status: int, results_count: int, req_remaining: int | None, req_limit: int | None) -> None:
    conn.execute(
        "INSERT INTO api_call (ts, endpoint, params_json, http_status, results_count, req_remaining, req_limit) "
        "VALUES (?,?,?,?,?,?,?)",
        (utcnow(), endpoint, json.dumps(params or {}, ensure_ascii=False),
         http_status, results_count, req_remaining, req_limit),
    )


def monthly_request_count(conn: sqlite3.Connection) -> int:
    """Quota-counting API calls in the current UTC month (budget tracking).

    Excludes ``/status`` — API-Football does not bill status checks.
    """
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM api_call WHERE substr(ts,1,7)=? AND endpoint NOT LIKE '%status%'",
        (month,),
    )
    return int(cur.fetchone()["n"])


def daily_request_count(conn: sqlite3.Connection) -> int:
    """Quota-counting API calls in the current UTC DAY — API-Football bills per day and
    resets at UTC midnight, so this is the real constraint (excludes /status)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM api_call WHERE substr(ts,1,10)=? AND endpoint NOT LIKE '%status%'",
        (day,),
    )
    return int(cur.fetchone()["n"])


if __name__ == "__main__":
    c = init_db()
    print(f"store ready at {DB_PATH}")
    print("tables:", [r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
    print("requests this month:", monthly_request_count(c))
