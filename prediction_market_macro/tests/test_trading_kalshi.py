"""§30 mirror executor — the failure modes the plan's D1 checklist pins.

The mirror is a follower: the paper ledger decides, this module copies at ×100 on the
DEMO account. What can go wrong is therefore not strategy but BOOKKEEPING, and every
test here is one of the ways a follower corrupts its book: double-sending, retro-
mirroring history, selling what it never bought, double-spending buying power,
running through a halt, or letting the two sides of the accounting identity drift.

Dark-mode contract (armed=False, the state until PR-9 fires): the FULL pipeline runs
and writes `dryrun` rows, but NO exchange write may ever happen — pinned here by a
poisoned place_taker_order.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import trading_kalshi as tk


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = init_db(tmp_path / "t.db")
    # no network from any test: prod-ask re-fetch is stubbed, order transport poisoned
    monkeypatch.setattr(tk, "_prod_ask", lambda ticker: 0.50)
    import prediction_market_macro.exec.kalshi_exec as kx
    def poisoned(*a, **k):
        raise AssertionError("exchange write attempted outside an armed test")
    monkeypatch.setattr(kx, "place_taker_order", poisoned)
    return c


def _paper_fill(conn, ticker="KXNATGASW-26AUG2117-T2.899", side="no", price=0.29,
                count=1, kind="open") -> int:
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot) VALUES(?,?,?,?,?,?,'{}','t/0','{}')",
        (datetime.now(timezone.utc).isoformat(), ticker.split("-", 1)[0], "2026-08-21",
         "{}", kind, price * count))
    f = conn.execute(
        "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
        " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
        (cur.lastrowid, datetime.now(timezone.utc).isoformat(), ticker, side, price,
         count, 0.02))
    conn.commit()
    return f.lastrowid


def _demo_fill(conn, fill_id, ticker, side, action, price, count, fee=0.0):
    conn.execute(
        "INSERT INTO demo_fills(fill_id, ticker, side, action, price, count, fee_usd,"
        " exchange_fill_id, ts) VALUES(?,?,?,?,?,?,?,?,?)",
        (fill_id, ticker, side, action, price, count, fee,
         f"x{fill_id}-{ticker}-{action}-{count}",
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ── the source pin: every fills door carries the inline hook ─────────────────

def test_every_fills_door_calls_the_inline_mirror():
    """`INSERT INTO fills` in production code must be followed by on_fill within a
    few lines — a new door added without the hook silently reopens the latency gap
    the inline design exists to close."""
    root = Path(__file__).resolve().parent.parent
    doors = []
    for p in root.rglob("*.py"):
        if "tests" in p.parts or p.name == "trading_kalshi.py":
            continue
        src = p.read_text()
        for m in re.finditer(r"INSERT INTO fills", src):
            tail = src[m.start():m.start() + 600]
            doors.append((str(p.relative_to(root)), "on_fill" in tail))
    assert doors, "no fills doors found — the scan itself broke"
    missing = [d for d, ok in doors if not ok]
    assert not missing, f"fills doors without inline mirror hook: {missing}"


# ── dark mode ────────────────────────────────────────────────────────────────

def test_dark_mode_full_pipeline_no_exchange_write(conn):
    fid = _paper_fill(conn)
    tk.on_fill(conn, fid)          # poisoned transport would raise on any POST
    row = conn.execute("SELECT * FROM demo_orders WHERE fill_id=?", (fid,)).fetchone()
    assert row["status"] == "dryrun"
    assert row["count_target"] == tk.MIRROR_MULT * 1
    assert row["client_order_id"] == f"spm-m{fid}"
    assert row["paper_ask"] == pytest.approx(0.29)
    assert row["prod_ask_at_send"] == pytest.approx(0.50)   # latency component captured


def test_idempotent_across_inline_and_sweep(conn):
    fid = _paper_fill(conn)
    tk.on_fill(conn, fid)
    tk.on_fill(conn, fid)
    tk.sync(conn)
    tk.sync(conn)
    n = conn.execute("SELECT COUNT(*) FROM demo_orders").fetchone()[0]
    assert n == 1


def test_watermark_blocks_retro_mirroring(conn):
    pre = _paper_fill(conn)
    tk._set_state(conn, "watermark", str(pre))   # deploy/arming after this fill
    tk.on_fill(conn, pre)
    assert conn.execute("SELECT COUNT(*) FROM demo_orders").fetchone()[0] == 0
    post = _paper_fill(conn)
    tk.on_fill(conn, post)
    assert conn.execute("SELECT fill_id FROM demo_orders").fetchone()[0] == post


def test_exit_clamps_to_held_and_never_shorts(conn):
    tk._set_state(conn, "watermark", "0")
    # paper closes 1 (=100 demo) but demo only ever bought 40
    fid_open = _paper_fill(conn, side="no", kind="open")
    _demo_fill(conn, fid_open, "KXNATGASW-26AUG2117-T2.899", "no", "buy", 0.29, 40)
    fid_close = _paper_fill(conn, side="close_no", kind="exit")
    tk.on_fill(conn, fid_close)
    row = conn.execute("SELECT * FROM demo_orders WHERE fill_id=?", (fid_close,)).fetchone()
    assert row["action"] == "sell" and row["side"] == "no"
    assert row["count_target"] == 40                        # clamped, not 100
    # and with zero held (pre-arming open), the exit is an explicit no-op row
    fid2 = _paper_fill(conn, ticker="KXWTIW-26AUG2114-B79.50", side="close_yes",
                       kind="exit")
    tk.on_fill(conn, fid2)
    assert conn.execute("SELECT status FROM demo_orders WHERE fill_id=?",
                        (fid2,)).fetchone()[0] == "skipped_noheld"


# ── armed mode (transport mocked per-test) ───────────────────────────────────

def _arm(conn, monkeypatch, cash=492.0, accept=True):
    tk.arm(conn, start_cash=cash)
    calls = []
    import prediction_market_macro.exec.kalshi_exec as kx
    from prediction_market_macro.exec.kalshi_exec import OrderResult
    def fake_order(c, **kw):
        calls.append(kw)
        return OrderResult("accepted", "ok", order_id=f"o{len(calls)}") if accept \
            else OrderResult("error", "rejected")
    monkeypatch.setattr(kx, "place_taker_order", fake_order)
    import prediction_market_macro.config.settings as st
    real = st.load_settings()
    class S:
        trading_enabled = True
        def __getattr__(self, k):
            return getattr(real, k)
    monkeypatch.setattr(st, "load_settings", lambda: S())
    monkeypatch.setenv("KALSHI_TRADING_ENABLED", "1")
    return calls


def test_armed_sends_with_mult_and_records_intent_then_sent(conn, monkeypatch):
    calls = _arm(conn, monkeypatch)
    fid = _paper_fill(conn, price=0.29, count=2)
    tk.on_fill(conn, fid)
    row = conn.execute("SELECT * FROM demo_orders WHERE fill_id=?", (fid,)).fetchone()
    assert row["status"] == "sent" and row["order_id"] == "o1"
    assert calls[0]["count"] == 200 and calls[0]["ref_price_cents"] == 29


def test_buying_power_scales_down_and_reserved_blocks_double_spend(conn, monkeypatch):
    calls = _arm(conn, monkeypatch, cash=100.0)
    fid1 = _paper_fill(conn, price=0.80, count=1)            # target 100 x 0.80 = $80
    tk.on_fill(conn, fid1)
    assert calls[0]["count"] == 100                          # affordable at $100
    # second fill same tick: reserved $80 leaves $20 -> 25 contracts at 0.80
    fid2 = _paper_fill(conn, ticker="KXWTIW-26AUG2114-B79.50", side="yes", price=0.80)
    tk.on_fill(conn, fid2)
    row2 = conn.execute("SELECT * FROM demo_orders WHERE fill_id=?", (fid2,)).fetchone()
    assert calls[1]["count"] == 25 and "power-scaled" in row2["note"]
    # third: nothing left after 100+25 reserved -> skipped_power, no order
    fid3 = _paper_fill(conn, ticker="KXCPI-26AUG-T0.2", side="yes", price=0.80)
    tk.on_fill(conn, fid3)
    assert conn.execute("SELECT status FROM demo_orders WHERE fill_id=?",
                        (fid3,)).fetchone()[0] == "skipped_power"
    assert len(calls) == 2


def test_halt_blocks_and_ack_restores(conn, monkeypatch):
    calls = _arm(conn, monkeypatch)
    tk.halt(conn, "test drift")
    fid = _paper_fill(conn)
    tk.on_fill(conn, fid)
    assert conn.execute("SELECT status FROM demo_orders WHERE fill_id=?",
                        (fid,)).fetchone()[0] == "skipped_halt"
    assert not calls
    tk.ack_halt(conn)
    fid2 = _paper_fill(conn, ticker="KXWTIW-26AUG2114-B79.50", side="yes")
    tk.on_fill(conn, fid2)
    assert conn.execute("SELECT status FROM demo_orders WHERE fill_id=?",
                        (fid2,)).fetchone()[0] == "sent"


def test_series_kill_switch(conn, monkeypatch):
    calls = _arm(conn, monkeypatch)
    tk._set_state(conn, "series_off:KXNATGASW", "1")
    fid = _paper_fill(conn)
    tk.on_fill(conn, fid)
    assert conn.execute("SELECT status FROM demo_orders WHERE fill_id=?",
                        (fid,)).fetchone()[0] == "skipped_gate"
    assert not calls


def test_gate1_env_switch_blocks_sends(conn, monkeypatch):
    calls = _arm(conn, monkeypatch)
    monkeypatch.delenv("KALSHI_TRADING_ENABLED")
    fid = _paper_fill(conn)
    tk.on_fill(conn, fid)
    assert conn.execute("SELECT status FROM demo_orders WHERE fill_id=?",
                        (fid,)).fetchone()[0] == "skipped_gate"
    assert not calls


# ── the balance sheet (§30.4) ────────────────────────────────────────────────

def test_accounting_identity_through_a_full_lifecycle(conn):
    """buy 100 → partial sell 40 → settlement of the rest; identity holds to the
    cent at every step and realized/fees accumulate correctly."""
    tk._set_state(conn, "start_cash", "492.00")
    t = "KXNATGASW-26AUG2117-T2.899"
    conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?)",
                 ("2026-08-18T00:00:00", t, 0.05, 0.11, 100, 100))
    _demo_fill(conn, 1, t, "no", "buy", 0.29, 100, fee=1.50)
    s1 = tk.snapshot_balance_sheet(conn)
    assert s1["positions_cost"] == pytest.approx(29.0)
    # NO-side mark: yes-mid 0.08 -> no 0.92
    assert s1["positions_mtm"] == pytest.approx(92.0)
    assert s1["cash_expected"] == pytest.approx(492 - 29 - 1.50)
    assert s1["equity"] == pytest.approx(s1["cash_exchange"] + 92.0)
    _demo_fill(conn, 2, t, "no", "sell", 0.60, 40, fee=0.70)
    s2 = tk.snapshot_balance_sheet(conn)
    assert s2["realized_cum"] == pytest.approx(40 * (0.60 - 0.29), abs=1e-6)
    assert s2["positions_cost"] == pytest.approx(60 * 0.29)
    conn.execute("INSERT INTO settlements VALUES(?,?,?,?,?,?)",
                 (t, "KXNATGASW", "26AUG2117", "no", "2026-08-21T21:00:00",
                  "2026-08-21T21:00:00"))
    conn.commit()
    s3 = tk.snapshot_balance_sheet(conn)
    # settled NO win: remaining 60 pay $1 each; realized = 40*0.60 + 60*1.00 - 100*0.29
    assert s3["realized_cum"] == pytest.approx(40 * 0.60 + 60 * 1.00 - 29.0, abs=1e-6)
    assert s3["n_open_positions"] == 0
    assert s3["cash_expected"] == pytest.approx(492 + s3["realized_cum"] - 2.20)
    assert s3["drift_usd"] == 0.0


def test_positions_aggregation_matches_hand_derivation(conn):
    t1, t2 = "KXWTIW-26AUG2114-B79.50", "KXWTIW-26AUG2114-B80.50"
    _demo_fill(conn, 1, t1, "yes", "buy", 0.30, 50)
    _demo_fill(conn, 2, t1, "yes", "buy", 0.40, 50)
    _demo_fill(conn, 3, t1, "yes", "sell", 0.50, 30)
    _demo_fill(conn, 4, t2, "no", "buy", 0.90, 10)
    pos = {(p["ticker"], p["side"]): p for p in tk.demo_positions(conn)}
    p1 = pos[(t1, "yes")]
    assert p1["count"] == 70 and p1["avg_cost"] == pytest.approx(0.35)
    assert pos[(t2, "no")]["count"] == 10


def test_armed_drift_beyond_tolerance_halts(conn, monkeypatch):
    tk.arm(conn, start_cash=492.0)
    tk.snapshot_balance_sheet(conn, cash_exchange=492.0)      # identity holds
    assert tk.halted(conn) is None
    tk.snapshot_balance_sheet(conn, cash_exchange=480.0)      # $12 unexplained
    assert tk.halted(conn) is not None and "drift" in tk.halted(conn)


def test_paper_ledger_untouched_by_dark_mirroring(conn):
    fid = _paper_fill(conn)
    before = conn.execute("SELECT * FROM fills").fetchall()
    tk.on_fill(conn, fid)
    tk.sync(conn)
    after = conn.execute("SELECT * FROM fills").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE kind NOT IN"
                        " ('open','exit')").fetchone()[0] == 0


# ── §30.4 transfers ledger (the 2026-08-20 top-up gap) ───────────────────────

def test_post_arm_deposit_halts_then_heals_through_the_ledger(conn, monkeypatch):
    """The exact scenario the top-up exposed, end to end: a benign deposit after arming
    IS unexplained drift until a human records it — then the identity heals, the halt is
    acked with a paper trail, and nothing was auto-explained."""
    tk.arm(conn, start_cash=492.0)
    tk.snapshot_balance_sheet(conn, cash_exchange=492.0)
    assert tk.halted(conn) is None
    # user tops up $2,207.35 at the exchange; the mirror knows nothing
    tk.snapshot_balance_sheet(conn, cash_exchange=2700.0 - 0.65)
    assert tk.halted(conn) is not None and "drift" in tk.halted(conn)
    rep = tk.record_transfer(conn, 2207.35, "deposit", "user top-up 2026-08-20")
    assert rep["transfers_net"] == pytest.approx(2207.35)
    s = tk.snapshot_balance_sheet(conn, cash_exchange=2700.0 - 0.65)
    assert s["drift_usd"] == 0.0
    assert s["transfers_cum"] == pytest.approx(2207.35)
    tk.ack_halt(conn)
    assert tk.halted(conn) is None
    # the paper trail: a ledger row AND an alert, neither optional
    assert conn.execute("SELECT COUNT(*) FROM demo_transfers").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE message LIKE"
                        " 'TRANSFER recorded%'").fetchone()[0] == 1


def test_withdrawal_is_signed_and_shrinks_buying_power(conn):
    """Withdrawals subtract: net = deposits − withdrawals, and cash_expected follows.
    A withdrawal the guard ignored would let the mirror spend money that left."""
    tk._set_state(conn, "start_cash", "492.00")
    tk.record_transfer(conn, 100.0, "deposit", "test deposit")
    tk.record_transfer(conn, 30.0, "withdrawal", "test withdrawal")
    assert tk._transfers_net(conn) == pytest.approx(70.0)
    s = tk.snapshot_balance_sheet(conn)
    assert s["cash_expected"] == pytest.approx(492.0 + 70.0)


def test_transfer_validation_refuses_the_unexplainable(conn):
    """No sign smuggling through amount, no unknown kinds, and above all no empty note —
    a transfer with no reason on record is exactly what the identity exists to catch."""
    with pytest.raises(ValueError):
        tk.record_transfer(conn, -50.0, "deposit", "negative amount")
    with pytest.raises(ValueError):
        tk.record_transfer(conn, 0.0, "deposit", "zero amount")
    with pytest.raises(ValueError):
        tk.record_transfer(conn, 50.0, "adjustment", "unknown kind")
    with pytest.raises(ValueError):
        tk.record_transfer(conn, 50.0, "deposit", "   ")
    assert conn.execute("SELECT COUNT(*) FROM demo_transfers").fetchone()[0] == 0


def test_transfers_do_not_touch_pnl_attribution(conn):
    """realized_cum must not move when capital moves — the ledger separates 'capital
    injected' from 'PnL earned', which is why it beats a start_cash rebase."""
    tk._set_state(conn, "start_cash", "492.00")
    s0 = tk.snapshot_balance_sheet(conn)
    tk.record_transfer(conn, 1000.0, "deposit", "capital injection")
    s1 = tk.snapshot_balance_sheet(conn)
    assert s1["realized_cum"] == s0["realized_cum"] == 0.0
    assert s1["cash_expected"] - s0["cash_expected"] == pytest.approx(1000.0)


def test_migration_adds_transfers_cum_to_an_existing_balance_sheet(tmp_path):
    """The live db predates the column; init_db must ALTER it in (the #149 lesson:
    IF-NOT-EXISTS DDL only reaches a fresh db)."""
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE demo_balance_sheet(
        ts TEXT PRIMARY KEY, cash_exchange REAL NOT NULL, cash_expected REAL NOT NULL,
        reserved_usd REAL NOT NULL, positions_cost REAL NOT NULL,
        positions_mtm REAL NOT NULL, equity REAL NOT NULL, realized_cum REAL NOT NULL,
        fees_cum REAL NOT NULL, n_open_positions INTEGER NOT NULL,
        exposure_json TEXT NOT NULL, drift_usd REAL NOT NULL)""")
    c.execute("INSERT INTO demo_balance_sheet VALUES('2026-08-18T00:00:00',492,492,0,"
              "0,0,492,0,0,0,'{}',0)")
    c.commit(); c.close()
    conn2 = init_db(p)
    cols = {r[1] for r in conn2.execute("PRAGMA table_info(demo_balance_sheet)")}
    assert "transfers_cum" in cols
    # old rows read back at 0.0, not NULL
    assert conn2.execute("SELECT transfers_cum FROM demo_balance_sheet"
                         ).fetchone()[0] == 0.0
    conn2.close()
